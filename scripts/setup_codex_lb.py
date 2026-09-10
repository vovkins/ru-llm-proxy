#!/usr/bin/env python3
"""Finish codex-lb bootstrap through its stock dashboard API."""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import os
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


SERVICE_KEY_NAME = "ru-llm-proxy-litellm"
PLACEHOLDERS = {"", "***"}


class CodexLBError(RuntimeError):
    """A user-facing codex-lb setup failure."""


class CodexLBClient:
    def __init__(self, base_url: str, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        cookie_jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar)
        )

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Optional[Dict[str, Any]] = None,
        bearer: Optional[str] = None,
    ) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"

        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise CodexLBError(
                f"codex-lb вернул HTTP {exc.code} для {method} {path}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise CodexLBError(
                f"codex-lb недоступен по адресу {self.base_url}: {exc.reason}"
            ) from exc

        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise CodexLBError(
                f"codex-lb вернул некорректный JSON для {method} {path}"
            ) from exc


def read_env_value(path: Path, key: str) -> str:
    if not path.exists():
        return ""
    prefix = f"{key}="
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    return ""


def update_env_value(path: Path, key: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise CodexLBError(f"Значение {key} содержит перевод строки")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    prefix = f"{key}="
    replacement = f"{key}={value}\n"
    replaced = False
    updated: List[str] = []

    for line in lines:
        if line.startswith(prefix):
            updated.append(replacement)
            replaced = True
        else:
            updated.append(line)

    if not replaced:
        if updated and not updated[-1].endswith("\n"):
            updated[-1] += "\n"
        updated.append(replacement)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as temporary:
            temporary.writelines(updated)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def extract_accounts(response: Any) -> List[Dict[str, Any]]:
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if isinstance(response, dict):
        for key in ("items", "accounts", "data"):
            value = response.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    raise CodexLBError("Не удалось определить список подписок в ответе codex-lb")


def choose_service_key(
    keys: Iterable[Dict[str, Any]], current_key: str
) -> Optional[Dict[str, Any]]:
    candidates = [item for item in keys if item.get("name") == SERVICE_KEY_NAME]
    for item in candidates:
        prefix = item.get("keyPrefix")
        if (
            current_key not in PLACEHOLDERS
            and isinstance(prefix, str)
            and current_key.startswith(prefix)
        ):
            return item
    return candidates[0] if candidates else None


def configure(client: CodexLBClient, env_path: Path, password: str) -> int:
    client.request(
        "/api/dashboard-auth/password/login",
        method="POST",
        payload={"password": password},
    )

    accounts = extract_accounts(client.request("/api/accounts"))
    if not accounts:
        raise CodexLBError(
            "В codex-lb нет ни одной подписки. Импортируйте подписку через панель "
            "и повторите make setup."
        )

    settings = client.request("/api/settings")
    if not isinstance(settings, dict) or not isinstance(settings.get("version"), int):
        raise CodexLBError("codex-lb не вернул версию административных настроек")
    if settings.get("apiKeyAuthEnabled") is not True:
        client.request(
            "/api/settings",
            method="PUT",
            payload={
                "expectedVersion": settings["version"],
                "apiKeyAuthEnabled": True,
            },
        )

    listed_keys = client.request("/api/api-keys/")
    if not isinstance(listed_keys, list):
        raise CodexLBError("codex-lb не вернул список служебных ключей")

    current_key = read_env_value(env_path, "CODEX_LB_API_KEY")
    service_key = choose_service_key(listed_keys, current_key)
    full_key = current_key

    if service_key is None:
        created = client.request(
            "/api/api-keys/",
            method="POST",
            payload={"name": SERVICE_KEY_NAME},
        )
        full_key = created.get("key") if isinstance(created, dict) else ""
    else:
        key_id = service_key.get("id")
        if not isinstance(key_id, str) or not key_id:
            raise CodexLBError("У существующего служебного ключа отсутствует id")
        if service_key.get("isActive") is not True:
            client.request(
                f"/api/api-keys/{urllib.parse.quote(key_id, safe='')}",
                method="PATCH",
                payload={"isActive": True},
            )
        prefix = service_key.get("keyPrefix")
        if (
            current_key in PLACEHOLDERS
            or not isinstance(prefix, str)
            or not current_key.startswith(prefix)
        ):
            regenerated = client.request(
                f"/api/api-keys/{urllib.parse.quote(key_id, safe='')}/regenerate",
                method="POST",
            )
            full_key = regenerated.get("key") if isinstance(regenerated, dict) else ""

    if not isinstance(full_key, str) or full_key in PLACEHOLDERS:
        raise CodexLBError("codex-lb не вернул полный служебный ключ")

    models = client.request("/v1/models", bearer=full_key)
    if (
        not isinstance(models, dict)
        or not isinstance(models.get("data"), list)
        or not models["data"]
    ):
        raise CodexLBError(
            "Новый служебный ключ не прошёл проверку /v1/models: "
            "каталог моделей пуст или недоступен"
        )

    update_env_value(env_path, "CODEX_LB_API_KEY", full_key)
    return len(accounts)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--base-url", default="http://127.0.0.1:2455")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env_path = Path(args.env_file)
    print("Откройте панель codex-lb, создайте администратора и импортируйте подписки.")
    input("После завершения нажмите Enter, чтобы продолжить настройку: ")
    password = getpass.getpass("Пароль администратора codex-lb: ")
    try:
        account_count = configure(CodexLBClient(args.base_url), env_path, password)
    except CodexLBError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    finally:
        password = ""

    print(
        "✅ Аутентификация API включена, служебный ключ сохранён в .env; "
        f"доступных подписок: {account_count}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
