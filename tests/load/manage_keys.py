#!/usr/bin/env python3
"""Create and remove temporary LiteLLM virtual keys for load tests."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


DEFAULT_KEY_FILE = Path("/state/keys.json")


def request_json(
    base_url: str,
    path: str,
    *,
    master_key: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 30,
) -> object:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        method="POST" if body is not None else "GET",
        headers={
            "Authorization": f"Bearer {master_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".keys-")
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary_name, path)
        path.chmod(0o600)
        owner_uid = os.getenv("LOAD_KEY_FILE_UID")
        owner_gid = os.getenv("LOAD_KEY_FILE_GID", owner_uid or "")
        if owner_uid and owner_gid:
            os.chown(path, int(owner_uid), int(owner_gid))
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def read_state(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
        raise ValueError("invalid load key state")
    if not all(isinstance(key, str) and key for key in payload["keys"]):
        raise ValueError("invalid key in load key state")
    return payload


def create_key(
    base_url: str,
    master_key: str,
    *,
    run_id: str,
    index: int,
    model: str,
    duration: str,
) -> str:
    response = request_json(
        base_url,
        "/key/generate",
        master_key=master_key,
        payload={
            "key_alias": f"load-{run_id}-{index:04d}",
            "models": [model],
            "duration": duration,
            "metadata": {
                "purpose": "load-test",
                "run_id": run_id,
                "user_index": index,
            },
        },
    )
    if not isinstance(response, dict):
        raise RuntimeError("LiteLLM returned an invalid key response")
    key = response.get("key") or response.get("token") or response.get("api_key")
    if not isinstance(key, str) or not key:
        raise RuntimeError("LiteLLM response did not contain a virtual key")
    return key


def create_keys(args: argparse.Namespace) -> None:
    if args.count < 1 or args.count > 10_000:
        raise ValueError("count must be between 1 and 10000")
    if args.concurrency < 1 or args.concurrency > 32:
        raise ValueError("concurrency must be between 1 and 32")
    if args.key_file.exists():
        raise FileExistsError(f"{args.key_file} already exists; delete its keys first")

    run_id = uuid.uuid4().hex[:12]
    state: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "model": args.model,
        "created_at": int(time.time()),
        "keys": [],
    }
    write_state(args.key_file, state)

    def create(index: int) -> tuple[int, str]:
        return index, create_key(
            args.base_url,
            args.master_key,
            run_id=run_id,
            index=index,
            model=args.model,
            duration=args.duration,
        )

    generated: dict[int, str] = {}
    errors: list[BaseException] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.concurrency
    ) as executor:
        futures = [executor.submit(create, index) for index in range(args.count)]
        for future in concurrent.futures.as_completed(futures):
            try:
                index, key = future.result()
                generated[index] = key
                state["keys"] = [generated[item] for item in sorted(generated)]
                write_state(args.key_file, state)
            except BaseException as exc:
                errors.append(exc)

    if errors:
        print(
            f"Key generation stopped after {len(state['keys'])} keys; "
            "the state file was retained for cleanup.",
            flush=True,
        )
        raise RuntimeError(f"failed to generate {len(errors)} virtual keys") from errors[0]

    print(f"Created {len(state['keys'])} temporary virtual keys.", flush=True)


def delete_keys(args: argparse.Namespace) -> None:
    if not args.key_file.exists():
        print("No temporary key state found.", flush=True)
        return
    state = read_state(args.key_file)
    keys = list(state["keys"])
    deleted_count = 0
    while keys:
        batch = keys[:50]
        request_json(
            args.base_url,
            "/key/delete",
            master_key=args.master_key,
            payload={"keys": batch},
        )
        deleted_count += len(batch)
        keys = keys[len(batch) :]
        state["keys"] = keys
        write_state(args.key_file, state)
    args.key_file.unlink()
    print(f"Deleted {deleted_count} temporary virtual keys.", flush=True)


def wait_until_ready(base_url: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(f"{base_url.rstrip('/')}/health/liveliness")
            with urllib.request.urlopen(request, timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(2)
    raise TimeoutError(f"LiteLLM did not become ready within {timeout} seconds")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "delete"))
    parser.add_argument(
        "--base-url",
        default=os.getenv("LOAD_TARGET_URL", "http://load-litellm:4000"),
    )
    parser.add_argument("--master-key", default=os.getenv("LOAD_MASTER_KEY"))
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    parser.add_argument("--count", type=int, default=int(os.getenv("LOAD_USERS", "400")))
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--model", default=os.getenv("LOAD_MODEL", "mock-chat"))
    parser.add_argument("--duration", default="2h")
    parser.add_argument("--ready-timeout", type=int, default=180)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.master_key:
        raise SystemExit("LOAD_MASTER_KEY is required")
    wait_until_ready(args.base_url, args.ready_timeout)
    if args.action == "create":
        create_keys(args)
    else:
        delete_keys(args)


if __name__ == "__main__":
    main()
