#!/usr/bin/env python3
"""Shared context for the project's single-server Compose deployments."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

from setup_codex_lb import update_env_value


ROOT = Path(__file__).resolve().parents[1]
STACKS = {"litellm-presidio", "litellm-presidio-codex-lb"}
RESOURCE_COMPONENTS = ("LITELLM", "ANALYZER", "NGINX", "REDIS", "DB", "CODEX_LB", "CODEX_LB_DB")
RESOURCE_KEYS = {f"{name}_{suffix}" for name in RESOURCE_COMPONENTS for suffix in ("CPUS", "MEMORY")}
RESOURCE_KEYS |= {"LITELLM_REPLICAS", "ANALYZER_REPLICAS"}


class DeploymentError(RuntimeError):
    pass


def setting(path: Path, key: str, default: str = "") -> str:
    """Read a scalar setting without executing the administrator's env file."""
    if not path.exists():
        return default
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*(?:export\s+)?" + re.escape(key) + r"\s*=\s*(.*)$", line)
        if match:
            lexer = shlex.shlex(match[1], posix=True)
            lexer.whitespace_split = True
            lexer.commenters = ""
            try:
                parts = list(lexer)
            except ValueError as exc:
                raise DeploymentError(f"Некорректная запись {key} в {path}") from exc
            if len(parts) > 1 and parts[1].startswith("#"):
                parts = parts[:1]
            if len(parts) > 1:
                raise DeploymentError(f"Некорректное скалярное значение {key} в {path}")
            values.append(parts[0] if parts else "")
    if len(values) > 1:
        raise DeploymentError(f"Повторная запись {key} в {path}")
    return values[0] if values else default


def memory_bytes(value: str) -> int:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([bkmgt](?:i?b)?)?", value.lower())
    if not match:
        raise DeploymentError("Память должна быть положительным размером, например 4g")
    exponent = "bkmgt".index((match[2] or "b")[0])
    size = int(Decimal(match[1]) * (1024 ** exponent))
    if size <= 0:
        raise DeploymentError("Лимит памяти должен быть больше нуля")
    return size


def resources(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise DeploymentError(f"Файл топологии не найден: {path}")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=", line)
        if not match or match[1] not in RESOURCE_KEYS:
            raise DeploymentError(f"В файле топологии допустимы только количества и ресурсы: {path}")
        key = match[1]
        if key in values:
            raise DeploymentError(f"Повторная запись {key} в {path}")
        value = setting(path, key)
        if key.endswith("REPLICAS"):
            if not re.fullmatch(r"[1-9][0-9]*", value):
                raise DeploymentError(f"{key}: требуется положительное целое число")
        elif key.endswith("CPUS"):
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) or Decimal(value) <= 0:
                raise DeploymentError(f"{key}: требуется положительное число CPU")
        else:
            memory_bytes(value)
        values[key] = value
    return values


class Context:
    def __init__(self, *, stack: str | None = None, env_file: str | None = None):
        self.stack = stack or os.getenv("STACK", "litellm-presidio")
        self.topology = os.getenv("TOPOLOGY", "functional")
        self.profile = os.getenv("ANALYZER_PROFILE", "cpu")
        self.env_file = Path(env_file or os.getenv("ENV_FILE", ".env"))
        if self.stack not in STACKS:
            raise DeploymentError("Укажите STACK=litellm-presidio или STACK=litellm-presidio-codex-lb")
        if self.topology not in {"functional", "production"}:
            raise DeploymentError("TOPOLOGY должен быть functional или production")
        if self.profile not in {"cpu", "gpu"}:
            raise DeploymentError("ANALYZER_PROFILE должен быть cpu или gpu")
        if self.topology == "production" and self.profile != "cpu":
            raise DeploymentError("Production-профиль этой версии поддерживает только CPU")
        self.explicit_mode = os.getenv("PII_MODE_ORIGIN") == "command line"
        raw_mode = os.environ["PII_GUARDRAIL_MODE"] if "PII_GUARDRAIL_MODE" in os.environ else setting(self.env_file, "PII_GUARDRAIL_MODE", "mask")
        self.mode = raw_mode.strip().lower()
        if self.mode not in {"mask", "block"}:
            raise DeploymentError("PII_GUARDRAIL_MODE должен быть MASK или BLOCK; пустое значение запрещено")
        self.values = {}
        self.files = ["docker-compose.yml"]
        if self.profile == "gpu":
            self.files.append("docker-compose.gpu.yml")
        if self.extended:
            self.files.append("docker-compose.codex-lb.yml")
        if self.topology == "production":
            self.values = resources(ROOT / "deploy/topologies/production.env")
            if os.getenv("TOPOLOGY_ENV"):
                self.values.update(resources(Path(os.environ["TOPOLOGY_ENV"])))
            self.files.append("docker-compose.production.yml")
            if self.extended:
                self.files.append("docker-compose.production.codex-lb.yml")
        elif os.getenv("TOPOLOGY_ENV"):
            raise DeploymentError("TOPOLOGY_ENV применяется только к production")
        self.environment = dict(os.environ, **self.values)
        self.environment.update(PII_GUARDRAIL_MODE=self.mode, TOPOLOGY=self.topology)
        self._model = None
        try:
            self.start_timeout = int(os.getenv("STACK_START_TIMEOUT", "180"))
        except ValueError as exc:
            raise DeploymentError("STACK_START_TIMEOUT должен быть положительным целым числом") from exc
        if self.start_timeout <= 0:
            raise DeploymentError("STACK_START_TIMEOUT должен быть положительным целым числом")

    @property
    def extended(self):
        return self.stack == "litellm-presidio-codex-lb"

    @property
    def counts(self):
        counts = {"nginx": 1, "db": 1, "redis": 1, "litellm": 1, "presidio-analyzer": 1}
        if self.topology == "production":
            counts.update(litellm=int(self.values["LITELLM_REPLICAS"]), **{"presidio-analyzer": int(self.values["ANALYZER_REPLICAS"])})
        if self.extended:
            counts.update({"codex-lb": 1, "codex-lb-db": 1})
        return counts

    @property
    def command(self):
        command = ["docker", "compose", "--env-file", str(self.env_file)]
        for file in self.files:
            command += ["-f", file]
        return command

    def compose(self, *args, capture=False):
        result = subprocess.run(self.command + list(args), env=self.environment, cwd=ROOT, text=True, capture_output=capture)
        if result.returncode:
            # Do not relay config output or environment values containing credentials.
            raise DeploymentError(f"Docker Compose завершился с кодом {result.returncode}")
        return result.stdout if capture else None

    @property
    def model(self):
        if self._model is None:
            self._model = json.loads(self.compose("config", "--format", "json", capture=True))
        return self._model

    def container_ids(self):
        output = subprocess.check_output(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={self.model['name']}", "--filter", "label=com.docker.compose.oneoff=False"], text=True)
        return output.split()

    def containers(self):
        ids = self.container_ids()
        if not ids:
            return []
        return json.loads(subprocess.check_output(["docker", "inspect", *ids], text=True))

    def guard(self, operation):
        if operation not in {"preflight", "mutation", "switch"}:
            raise DeploymentError("Неизвестная операция проверки состава")
        if operation == "preflight":
            if not self.env_file.is_file():
                raise DeploymentError(f"Файл {self.env_file} не найден; выполните make setup")
            required = ["LITELLM_MASTER_KEY", "LITELLM_SALT_KEY", "POSTGRES_PASSWORD", "UI_PASSWORD"]
            if self.extended:
                required += ["CODEX_LB_POSTGRES_PASSWORD", "CODEX_LB_API_KEY"]
            for key in required:
                if setting(self.env_file, key) in {"", "***", "sk-replace-with-generated-key", "replace-with-generated-salt", "replace-with-generated-ui-password"}:
                    raise DeploymentError(f"В {self.env_file} не настроена {key}; выполните make setup")
            return
        for container in self.containers():
            labels = container["Config"]["Labels"]
            if not self.extended and labels.get("com.docker.compose.service") in {"codex-lb", "codex-lb-db"} and container["State"]["Running"]:
                raise DeploymentError("Сейчас запущен расширенный состав; укажите STACK=litellm-presidio-codex-lb")
            actual = labels.get("ru-llm-proxy.topology", "functional")
            if operation == "mutation" and labels.get("com.docker.compose.service") in self.counts and actual != self.topology:
                raise DeploymentError(f"Контейнеры имеют TOPOLOGY={actual}; используйте его для down/clean, а для переключения — up/restart")

    def summary(self):
        print(f"Состав: {self.stack}; топология: {self.topology}; режим: {self.mode.upper()}", flush=True)
        print("Экземпляры: " + ", ".join(f"{service}={count}" for service, count in self.counts.items()), flush=True)
        if self.topology == "production":
            cpu, memory = Decimal(0), 0
            for service, count in self.counts.items():
                spec = self.model["services"][service]
                cpu += Decimal(str(spec["cpus"])) * count
                memory += int(spec["mem_limit"]) * count
            print(f"Суммарные лимиты: {cpu} CPU / {memory / 1024**3:g} GiB (не резервирование ресурсов)", flush=True)

    def persist_mode(self):
        if self.explicit_mode:
            update_env_value(self.env_file, "PII_GUARDRAIL_MODE", self.mode, preserve_permissions=True)

    def setup(self):
        # Validate Compose and the running context before generating any secrets.
        original_file, original_environment = self.env_file, self.environment
        try:
            if not self.env_file.exists():
                self.env_file = ROOT / ".env.example"
            self.environment = dict(self.environment)
            self.environment.setdefault("CODEX_LB_POSTGRES_PASSWORD", "setup-only")
            self.environment.setdefault("CODEX_LB_API_KEY", "setup-only")
            self.guard("mutation")
            for item in self.containers():
                if item["State"]["Running"] and item["Config"]["Labels"].get("com.docker.compose.service") == "litellm":
                    mode = next((entry.split("=", 1)[1].lower() for entry in item["Config"]["Env"] if entry.startswith("PII_GUARDRAIL_MODE=")), "mask")
                    if mode != self.mode:
                        raise DeploymentError("Перед setup с другой политикой остановите текущий состав через make down")
        finally:
            self.env_file, self.environment, self._model = original_file, original_environment, None
        subprocess.run(["bash", "scripts/setup_env.sh", str(self.env_file), ".env.example", self.stack], cwd=ROOT, check=True)
        self.persist_mode()
        self.summary()
        if self.extended:
            subprocess.run(["bash", "scripts/setup_codex_lb.sh", str(self.env_file)], cwd=ROOT, env=self.environment, check=True)

    def health(self, wait=0, services=None):
        if wait < 0:
            raise DeploymentError("Время ожидания готовности не может быть отрицательным")
        deadline = time.monotonic() + wait
        wanted = services or self.counts
        while True:
            containers = self.containers()
            failures = []
            for service, count in wanted.items():
                found = [item for item in containers if item["Config"]["Labels"].get("com.docker.compose.service") == service]
                if len(found) != count:
                    failures.append(f"{service}: ожидается {count}, найдено {len(found)}")
                for item in found:
                    state = item["State"]
                    if not state["Running"] or state.get("Health", {}).get("Status") != "healthy":
                        failures.append(f"{item['Name'].lstrip('/')}: не готов")
                    elif service == "presidio-analyzer" and not self.ner_ready(item):
                        failures.append(f"{item['Name'].lstrip('/')}: NER не прогрета")
                    if service == "litellm":
                        mode = next((entry.split("=", 1)[1] for entry in item["Config"]["Env"] if entry.startswith("PII_GUARDRAIL_MODE=")), "mask")
                        if mode != self.mode:
                            failures.append(f"{item['Name'].lstrip('/')}: работает {mode.upper()}, ожидается {self.mode.upper()}")
            if not failures and services is None and not self.gateway_ready(containers):
                failures.append("Nginx: маршрут к LiteLLM или codex-lb не готов")
            if not failures:
                for item in containers:
                    service = item["Config"]["Labels"].get("com.docker.compose.service")
                    if service in wanted:
                        print(f"{item['Name'].lstrip('/')}: healthy", flush=True)
                return
            if time.monotonic() >= deadline:
                raise DeploymentError("; ".join(failures))
            time.sleep(1)

    def gateway_ready(self, containers):
        ingress = next(item for item in containers if item["Config"]["Labels"].get("com.docker.compose.service") == "nginx")
        paths = ["http://127.0.0.1/health/liveliness"]
        if self.extended:
            paths.append("http://127.0.0.1:2455/health/ready")
        for url in paths:
            result = subprocess.run(["docker", "exec", ingress["Id"], "wget", "-qO-", "-T", "5", url], capture_output=True)
            if result.returncode:
                return False
        return True

    def ner_ready(self, item):
        code = "import json, urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:5001/api/v1/health', timeout=5)); print(json.dumps(data))"
        result = subprocess.run(["docker", "exec", item["Id"], "python", "-c", code], text=True, capture_output=True)
        if result.returncode:
            return False
        try:
            data = json.loads(result.stdout)
            return data.get("ner_state") == "ready" and data.get("ner_warmed_up") is True
        except ValueError:
            return False

    def start(self, force=False, only_litellm=False):
        self.guard("preflight")
        self.guard("mutation" if only_litellm else "switch")
        self.model
        self.summary()
        previous = self.containers()
        serving = list(previous)
        timeout = self.start_timeout
        if not force:
            for service in ("litellm", "presidio-analyzer"):
                existing = [item for item in previous if item["Config"]["Labels"].get("com.docker.compose.service") == service]
                if existing:
                    expected_hash = self.compose("config", "--hash", service, capture=True).split()[-1]
                    changed = any(item["Config"]["Labels"].get("com.docker.compose.config-hash") != expected_hash for item in existing)
                    if not changed:
                        compatibility = self.environment["COMPOSE_COMPATIBILITY"] if "COMPOSE_COMPATIBILITY" in self.environment else setting(self.env_file, "COMPOSE_COMPATIBILITY")
                        separator = "_" if compatibility.lower() in {"1", "true", "t"} else "-"
                        image = self.model["services"][service].get("image") or f"{self.model['name']}{separator}{service}"
                        expected_image = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True, stderr=subprocess.DEVNULL).strip()
                        changed = any(item["Image"] != expected_image for item in existing)
                    if changed:
                        previous = [item for item in previous if item not in existing]
        if only_litellm:
            self.health(services={service: count for service, count in self.counts.items() if service != "litellm"})
        # Close ingress before changing the policy or topology of serving instances.
        if any(item["State"]["Running"] for item in self.containers()):
            self.compose("stop", "nginx")
        # Functional exposes LiteLLM directly too; close that ingress on policy switches.
        for item in serving:
            labels = item["Config"]["Labels"]
            if labels.get("com.docker.compose.service") != "litellm" or not item["State"]["Running"]:
                continue
            mode = next((entry.split("=", 1)[1].lower() for entry in item["Config"]["Env"] if entry.startswith("PII_GUARDRAIL_MODE=")), "mask")
            if force or mode != self.mode or labels.get("ru-llm-proxy.topology", "functional") != self.topology:
                self.compose("stop", "litellm")
                previous = [entry for entry in previous if entry["Config"]["Labels"].get("com.docker.compose.service") != "litellm"]
                break
        self.persist_mode()
        if force:
            previous = []
        flags = ["--force-recreate"] if force else []
        if not only_litellm:
            self.compose("up", "-d", "--no-build", *flags, "db", "redis")
            self.health(timeout, {"db": 1, "redis": 1})
        # Sequential cold starts keep checkpoint-loading peaks bounded.
        for count in ([] if only_litellm else self.start_counts(previous, "presidio-analyzer")):
            self.compose("up", "-d", "--no-build", "--no-deps", *flags, "--scale", f"presidio-analyzer={count}", "presidio-analyzer")
            self.health(timeout, {"presidio-analyzer": count})
            flags = []
        if self.extended and not only_litellm:
            self.compose("up", "-d", "--no-build", *(["--force-recreate"] if force else []), "codex-lb-db", "codex-lb")
            self.health(timeout, {"codex-lb-db": 1, "codex-lb": 1})
        flags = ["--force-recreate"] if force else []
        for count in self.start_counts(previous, "litellm"):
            self.compose("up", "-d", "--no-build", "--no-deps", *flags, "--scale", f"litellm={count}", "litellm")
            self.health(timeout, {"litellm": count})
            flags = []
        self.compose("up", "-d", "--no-build", "--no-deps", *(["--force-recreate"] if force else []), "nginx")
        self.health(timeout)

    def start_counts(self, previous, service):
        current = sum(item["State"]["Running"] for item in previous if item["Config"]["Labels"].get("com.docker.compose.service") == service)
        desired = self.counts[service]
        return range(max(1, current), desired + 1) if current < desired else [desired]

    def url(self):
        if os.getenv("LITELLM_URL"):
            return os.environ["LITELLM_URL"].rstrip("/")
        port = os.environ["LITELLM_PORT"] if "LITELLM_PORT" in os.environ else setting(self.env_file, "LITELLM_PORT", "4000")
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise DeploymentError("Некорректный LITELLM_PORT")
        return f"http://localhost:{port}"

    def metrics(self):
        self.guard("mutation")
        self.health()
        for item in self.containers():
            service = item["Config"]["Labels"].get("com.docker.compose.service")
            if service not in {"litellm", "presidio-analyzer"}:
                continue
            port = 4000 if service == "litellm" else 5001
            print(f"# {item['Name'].lstrip('/')} /metrics", flush=True)
            code = f"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:{port}/metrics', timeout=10).read().decode())"
            result = subprocess.check_output(["docker", "exec", item["Id"], "python", "-c", code], text=True)
            if not any(line.startswith(("litellm_", "ru_pii_guardrail_", "ru_presidio_analyzer_")) for line in result.splitlines()):
                raise DeploymentError(f"В {service} отсутствуют метрики")
            print("\n".join(result.splitlines()[:120]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack")
    parser.add_argument("--env-file")
    parser.add_argument("action", choices=["validate", "setup", "start", "guard", "health", "compose", "url", "metrics", "exec"])
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        context = Context(stack=args.stack, env_file=args.env_file)
        if args.action == "validate":
            return
        if args.action == "setup":
            context.setup()
        elif args.action == "start":
            context.start(force="--force-recreate" in args.args, only_litellm="--only-litellm" in args.args)
        elif args.action == "guard":
            context.guard(args.args[0])
        elif args.action == "health":
            context.guard("mutation")
            context.summary()
            context.health(int(args.args[0]) if args.args else 0)
        elif args.action == "compose":
            context.compose(*args.args)
        elif args.action == "url":
            print(context.url())
        elif args.action == "metrics":
            context.metrics()
        elif args.action == "exec":
            context.guard("mutation")
            environment = dict(context.environment)
            environment.update(ENV_FILE=str(context.env_file), LITELLM_URL=os.getenv("LITELLM_URL", context.url()), COMPOSE_FILE=os.pathsep.join(context.files), COMPOSE_ENV_FILES=str(context.env_file))
            subprocess.run(args.args, cwd=ROOT, env=environment, check=True)
    except (DeploymentError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"Ошибка развёртывания: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
