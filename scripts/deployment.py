#!/usr/bin/env python3
"""Shared configuration context for the project's Compose deployments."""

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
from node_config import NodeConfigurationError, load_node_configuration
import node_bundle


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from presidio.model_contract import REMOVED_SETTINGS, reject_removed_settings
STACKS = {"litellm-presidio", "litellm-presidio-codex-lb"}
NODE_ROLES = {"primary", "compute"}
READ_ONLY_CONFIG = {(), ("--quiet",), ("--format", "json"), ("--services",), ("--images",)}
PRIMARY_COMMANDS = {"update-litellm", "virtual-key-create", "client-auth-smoke", "guardrails-list",
                    "guardrails-smoke", "routing-smoke", "test-e2e", "monitor-smoke", "ner-evaluate"}
RESOURCE_COMPONENTS = ("LITELLM", "ANALYZER", "NGINX", "REDIS", "DB", "CODEX_LB", "CODEX_LB_DB", "TRITON")
RESOURCE_KEYS = {f"{name}_{suffix}" for name in RESOURCE_COMPONENTS for suffix in ("CPUS", "MEMORY")}
COUNT_KEYS = {"LITELLM_REPLICAS", "ANALYZER_REPLICAS", "TRITON_REPLICAS", "TRITON_MODEL_INSTANCES"}
RESOURCE_KEYS |= COUNT_KEYS


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
        if key in COUNT_KEYS:
            if not re.fullmatch(r"[1-9][0-9]*", value):
                raise DeploymentError(f"{key}: требуется положительное целое число")
            if key == "TRITON_MODEL_INSTANCES" and int(value) > 32:
                raise DeploymentError("TRITON_MODEL_INSTANCES: допустимо от 1 до 32")
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
        self.env_file = Path(env_file or os.getenv("ENV_FILE", ".env"))
        file_keys = set(re.findall(r"(?m)^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=",
                                  self.env_file.read_text() if self.env_file.is_file() else ""))
        try:
            reject_removed_settings(set(os.environ) | file_keys)
        except ValueError as error:
            raise DeploymentError(str(error) + "; удалите эти параметры, см. docs/configuration.md") from None
        if self.stack not in STACKS:
            raise DeploymentError("Укажите STACK=litellm-presidio или STACK=litellm-presidio-codex-lb")
        if self.topology not in {"functional", "production"}:
            raise DeploymentError("TOPOLOGY должен быть functional или production")
        self.node_role = os.environ.get("NODE_ROLE")
        if self.node_role is not None and self.node_role not in NODE_ROLES:
            raise DeploymentError("NODE_ROLE должен быть primary или compute; для одного узла не задавайте его")
        if self.node_role is not None and self.topology != "production":
            raise DeploymentError("NODE_ROLE применяется только к TOPOLOGY=production")
        if {"NODE_ROLE", "NODE_CONFIG"} & file_keys:
            raise DeploymentError("NODE_ROLE/NODE_CONFIG задаются контекстом команды, не через ENV_FILE")
        config_path = os.environ.get("NODE_CONFIG")
        if config_path is not None and (not config_path or self.node_role is None):
            raise DeploymentError("NODE_CONFIG требует явного NODE_ROLE и непустого пути")
        self.node_config_path = Path(config_path) if config_path is not None else None
        self._node_config = None
        self.explicit_mode = os.getenv("PII_MODE_ORIGIN") == "command line"
        if self.node_role == "compute" and ("PII_GUARDRAIL_MODE" in os.environ or "PII_GUARDRAIL_MODE" in file_keys):
            raise DeploymentError("PII_GUARDRAIL_MODE задаётся на primary, не на compute")
        raw_mode = os.environ["PII_GUARDRAIL_MODE"] if "PII_GUARDRAIL_MODE" in os.environ else setting(self.env_file, "PII_GUARDRAIL_MODE", "mask")
        self.mode = raw_mode.strip().lower()
        if self.mode not in {"mask", "block"}:
            raise DeploymentError("PII_GUARDRAIL_MODE должен быть MASK или BLOCK; пустое значение запрещено")
        self.values = {}
        self.files = ["docker-compose.yml"]
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
        if self.node_role == "compute":
            self.files = ["docker-compose.production.compute.yml"]
        elif self.node_role == "primary":
            self.files.append("docker-compose.production.primary.yml")
            if self.extended:
                self.files.append("docker-compose.production.primary.codex-lb.yml")
        self.environment = dict(os.environ, **self.values)
        self.environment.update(PII_GUARDRAIL_MODE=self.mode, TOPOLOGY=self.topology)
        for key in ("TRITON_REPLICAS", "TRITON_MODEL_INSTANCES", "TRITON_CPUS", "TRITON_MEMORY"):
            expected = self.values.get(key, {"TRITON_REPLICAS": "1", "TRITON_MODEL_INSTANCES": "2"}.get(key, ""))
            for raw in (os.getenv(key), setting(self.env_file, key) if key in file_keys else None):
                if raw is not None and raw != expected:
                    raise DeploymentError(f"{key}: противоречит топологии; используйте production.env/TOPOLOGY_ENV")
        self._validate_ner_settings()
        self._model = None
        self._identity = None
        self._bundle = None
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
    def node_config(self):
        if self.node_role is None or self.node_config_path is None:
            raise DeploymentError("Для проверки узла укажите NODE_ROLE и NODE_CONFIG")
        if self._node_config is None:
            try:
                config = load_node_configuration(self.node_config_path)
                node_bundle.endpoint(config)
                self._node_config = config
            except (NodeConfigurationError, node_bundle.BundleError) as error:
                raise DeploymentError(str(error)) from None
        return self._node_config

    def validate_operations(self, operations):
        if self.node_role is None:
            return
        if "clean-all" in operations:
            raise DeploymentError("STACK=all несовместим с NODE_ROLE; выберите локальный STACK")
        if self.node_role == "compute" and PRIMARY_COMMANDS.intersection(operations):
            raise DeploymentError("Команда LiteLLM доступна только на primary, не на compute")
        if not operations or {"setup", "up", "restart", "start"}.intersection(operations):
            self.node_config

    def require_primary(self):
        if self.node_role == "compute":
            raise DeploymentError("Команда LiteLLM доступна только на primary, не на compute")

    def _validate_ner_settings(self):
        rules = {
            "PRESIDIO_ANALYZER_WORKERS": (1, None, True),
            "PRESIDIO_ANALYZER_CONCURRENCY_LIMIT": (1, None, True),
            "PRESIDIO_ANALYZER_QUEUE_LIMIT": (0, None, True),
            "PRESIDIO_ANALYZER_NER_BATCH_SIZE": (1, 32, True),
            "PRESIDIO_ANALYZER_QUEUE_TIMEOUT_SECONDS": (0, None, False),
            "PRESIDIO_ANALYZER_TRITON_TIMEOUT_SECONDS": (0, 600, False),
        }
        for key, (minimum, maximum, integer) in rules.items():
            raw = os.environ.get(key, setting(self.env_file, key))
            if raw == "":
                continue
            try:
                value = Decimal(raw)
                valid = value.is_finite() and value >= minimum and (maximum is None or value <= maximum)
                valid = valid and (not integer or bool(re.fullmatch(r"[0-9]+", raw)))
                if key == "PRESIDIO_ANALYZER_TRITON_TIMEOUT_SECONDS":
                    valid = valid and value > 0
            except ArithmeticError:
                valid = False
            if not valid:
                raise DeploymentError(f"Некорректная эксплуатационная настройка {key}")

    @property
    def counts(self):
        if self.node_role == "compute":
            return {"nginx": 1, "presidio-analyzer": int(self.values["ANALYZER_REPLICAS"]),
                    "ner-triton": int(self.values["TRITON_REPLICAS"])}
        counts = {"nginx": 1, "db": 1, "redis": 1, "litellm": 1, "presidio-analyzer": 1}
        if self.topology == "production":
            counts.update(litellm=int(self.values["LITELLM_REPLICAS"]), **{"presidio-analyzer": int(self.values["ANALYZER_REPLICAS"])})
        counts["ner-triton"] = int(self.values.get("TRITON_REPLICAS", "1"))
        if self.extended:
            counts.update({"codex-lb": 1, "codex-lb-db": 1})
        return counts

    @property
    def command(self):
        command = ["docker", "compose", "--env-file", str(self.env_file)]
        for file in self.files:
            command += ["-f", file]
        if self._bundle is not None:
            command += ["-f", str(self._bundle.overlay)]
        return command

    def prepare_runtime(self):
        if self.node_role is None:
            return
        config = self.node_config
        self._bundle, self._model = None, None
        model = self.model
        self.guard_role(self.containers())
        try:
            self._bundle = node_bundle.prepare(ROOT, model["name"], self.node_role,
                                               config, model["services"]["nginx"]["image"])
        except (node_bundle.BundleError, OSError, subprocess.SubprocessError) as error:
            raise DeploymentError("Не удалось подготовить bundle Nginx; действующий комплект не применяется заново") from error
        self._model = None

    def load_runtime(self, validate_current=False):
        if self.node_role is None or self._bundle is not None:
            return
        project = self.model["name"]
        if not (node_bundle.location(ROOT, project, self.node_role) / "current.json").exists():
            return
        try:
            self._bundle = node_bundle.load(ROOT, project, self.node_role)
            if validate_current:
                config = self.node_config
                expected = node_bundle.payload(self.node_role, project, config,
                    node_bundle.render(ROOT, self.node_role, config))
                if node_bundle.checksum(expected) != self._bundle.digest:
                    raise node_bundle.BundleError("NODE_CONFIG или шаблон изменён; примените setup/up с текущим комплектом")
        except node_bundle.BundleError as error:
            raise DeploymentError(str(error)) from None
        self._model = None

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
            if self.node_role is not None:
                self.node_config
            if not self.env_file.is_file():
                raise DeploymentError(f"Файл {self.env_file} не найден; выполните make setup")
            if self.node_role == "compute":
                return
            required = ["LITELLM_MASTER_KEY", "LITELLM_SALT_KEY", "POSTGRES_PASSWORD", "UI_PASSWORD"]
            if self.extended:
                required += ["CODEX_LB_POSTGRES_PASSWORD", "CODEX_LB_API_KEY"]
            for key in required:
                if setting(self.env_file, key) in {"", "***", "sk-replace-with-generated-key", "replace-with-generated-salt", "replace-with-generated-ui-password"}:
                    raise DeploymentError(f"В {self.env_file} не настроена {key}; выполните make setup")
            return
        containers = self.containers()
        self.guard_role(containers)
        for container in containers:
            labels = container["Config"]["Labels"]
            if not self.extended and labels.get("com.docker.compose.service") in {"codex-lb", "codex-lb-db"} and container["State"]["Running"]:
                raise DeploymentError("Сейчас запущен расширенный состав; укажите STACK=litellm-presidio-codex-lb")
            actual = labels.get("ru-llm-proxy.topology", "functional")
            if operation == "mutation" and labels.get("com.docker.compose.service") in self.counts and actual != self.topology:
                raise DeploymentError(f"Контейнеры имеют TOPOLOGY={actual}; используйте его для down/clean, а для переключения — up/restart")

    def guard_role(self, containers):
        if not containers:
            return
        labels = [item["Config"]["Labels"] for item in containers]
        roles = {item.get("ru-llm-proxy.node-role") for item in labels} - {None}
        if self.node_role is None:
            if roles:
                raise DeploymentError("Проект имеет NODE_ROLE; укажите его явно")
            return
        if roles != {self.node_role}:
            raise DeploymentError("Роль существующего проекта не совпадает с NODE_ROLE; автоматическая миграция запрещена")
        if any(item.get("com.docker.compose.service") == "nginx"
               and item.get("ru-llm-proxy.node-role") != self.node_role for item in labels):
            raise DeploymentError("Вход существующего проекта имеет другую NODE_ROLE")
        allowed = set(self.model["services"])
        if any(item.get("com.docker.compose.service") not in allowed for item in labels):
            raise DeploymentError("В локальном проекте найдены компоненты другого состава")
        if any(item.get("ru-llm-proxy.node-role") != self.node_role for item in labels):
            raise DeploymentError("Локальная роль не может управлять компонентами другого узла")

    def summary(self):
        detail = f"роль: {self.node_role}" if self.node_role == "compute" else f"режим: {self.mode.upper()}"
        if self.node_role == "primary":
            detail += "; роль: primary"
        print(f"Состав: {self.stack}; топология: {self.topology}; {detail}", flush=True)
        print("Экземпляры: " + ", ".join(f"{service}={count}" for service, count in self.counts.items()), flush=True)
        if self.topology == "production":
            cpu, memory = Decimal(0), 0
            for service, count in self.counts.items():
                spec = self.model["services"][service]
                cpu += Decimal(str(spec["cpus"])) * count
                memory += int(spec["mem_limit"]) * count
            print(f"Суммарные лимиты: {cpu} CPU / {memory / 1024**3:g} GiB (не резервирование ресурсов)", flush=True)

    def persist_mode(self):
        if self.explicit_mode and self.node_role != "compute":
            update_env_value(self.env_file, "PII_GUARDRAIL_MODE", self.mode, preserve_permissions=True)

    def build(self):
        original_file, original_environment = self.env_file, self.environment
        original_bundle = self._bundle
        if original_bundle is not None:
            self._bundle, self._model = None, None
        try:
            if not self.env_file.exists():
                self.env_file = ROOT / (".env.compute.example" if self.node_role == "compute" else ".env.example")
            self.environment = dict(self.environment)
            if self.node_role != "compute":
                self.environment.update(CODEX_LB_API_KEY="build-only", CODEX_LB_POSTGRES_PASSWORD="build-only")
            services = self.model["services"]
            if self.node_role is not None:
                self.guard_role(self.containers())
            local = self.counts
            pull = [name for name in local if "build" not in services[name]]
            build = [name for name in local if "build" in services[name]]
            if pull:
                self.compose("pull", *pull)
            if build:
                self.compose("build", "--no-cache", *build)
        finally:
            self.env_file, self.environment, self._model = original_file, original_environment, None
            self._bundle = original_bundle

    def setup(self):
        if self.node_role is not None:
            self.node_config
        # Validate Compose and the running context before generating any secrets.
        original_file, original_environment = self.env_file, self.environment
        try:
            if not self.env_file.exists():
                self.env_file = ROOT / (".env.compute.example" if self.node_role == "compute" else ".env.example")
            self.environment = dict(self.environment)
            self.environment.setdefault("CODEX_LB_POSTGRES_PASSWORD", "setup-only")
            self.environment.setdefault("CODEX_LB_API_KEY", "setup-only")
            self.guard("mutation")
            for item in self.containers():
                if item["State"]["Running"] and item["Config"]["Labels"].get("com.docker.compose.service") == "litellm":
                    mode = next((entry.split("=", 1)[1].lower() for entry in item["Config"]["Env"] if entry.startswith("PII_GUARDRAIL_MODE=")), "mask")
                    if mode != self.mode:
                        raise DeploymentError("Перед setup с другой политикой остановите текущий состав через make down")
            self.prepare_runtime()
        finally:
            self.env_file, self.environment, self._model = original_file, original_environment, None
        if self.node_role == "compute":
            if not self.env_file.exists():
                try:
                    descriptor = os.open(self.env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                        stream.write((ROOT / ".env.compute.example").read_text(encoding="utf-8"))
                except OSError:
                    raise DeploymentError("Не удалось создать ENV_FILE compute") from None
            print("ENV_FILE compute готов; секреты и политика LiteLLM на этом узле не настраиваются", flush=True)
            return
        subprocess.run(["bash", "scripts/setup_env.sh", str(self.env_file), ".env.example", self.stack], cwd=ROOT, check=True)
        self.persist_mode()
        self.summary()
        if self.extended:
            subprocess.run(["bash", "scripts/setup_codex_lb.sh", str(self.env_file)], cwd=ROOT, env=self.environment, check=True)

    def health(self, wait=0, services=None):
        if wait < 0:
            raise DeploymentError("Время ожидания готовности не может быть отрицательным")
        deadline = time.monotonic() + wait
        while True:
            report = self.readiness(services)
            if not report["failures"]:
                self.print_readiness(report)
                return report
            if time.monotonic() >= deadline:
                self.print_readiness(report)
                raise DeploymentError("; ".join(report["failures"]))
            time.sleep(1)

    def diagnostic_scope(self):
        return {"scope": "local", "role": self.node_role or "single",
                "project": (self._model or {}).get("name", "unknown"),
                "remote_node": "not_checked" if self.node_role else "not_applicable",
                "configured_bundle": (self._bundle.digest if self._bundle is not None else
                                      "not_loaded" if self.node_role else "not_applicable")}

    def readiness(self, services=None):
        """Inspect local replicas; balanced routes never certify remote pools."""
        containers = self.containers()
        wanted = services or self.counts
        report = dict(self.diagnostic_scope(), services={}, routes={}, failures=[])
        for service, count in wanted.items():
            found = sorted((item for item in containers if item["Config"]["Labels"].get("com.docker.compose.service") == service),
                           key=lambda item: item["Name"])
            rows = []
            if len(found) != count:
                report["failures"].append(f"{service}: ожидается {count}, найдено {len(found)}")
            for item in found:
                state = item["State"]
                if not state["Running"]:
                    reason = "stopped"
                elif state.get("Health", {}).get("Status") != "healthy":
                    reason = "starting" if state.get("Health", {}).get("Status") == "starting" else "unhealthy"
                elif service == "presidio-analyzer":
                    reason = self.ner_readiness(item)
                elif service == "ner-triton":
                    reason = self.triton_readiness(item)
                else:
                    reason = "ready"
                if service == "nginx" and self.node_role and reason == "ready":
                    if self._bundle is None:
                        reason = "bundle_not_loaded"
                    elif item["Config"]["Labels"].get("ru-llm-proxy.node-config-sha256") != self._bundle.digest:
                        reason = "bundle_mismatch"
                if service == "litellm":
                    mode = next((entry.split("=", 1)[1] for entry in item["Config"]["Env"] if entry.startswith("PII_GUARDRAIL_MODE=")), "mask")
                    if mode != self.mode:
                        reason = "policy_mismatch"
                        actual = mode.upper() if mode in {"mask", "block"} else "UNKNOWN"
                        report["failures"].append(f"{item['Name'].lstrip('/')}: работает {actual}, ожидается {self.mode.upper()}")
                rows.append({"name": item["Name"].lstrip("/"), "reason": reason})
                if reason != "ready":
                    detail = {"presidio-analyzer": "NER не прогрета или комплект не подтверждён",
                              "ner-triton": "комплект Triton не подтверждён"}.get(service, "не готов")
                    report["failures"].append(f"{item['Name'].lstrip('/')}: не готов ({detail}; {reason})")
            report["services"][service] = {"expected": count, "found": len(found),
                "ready": sum(row["reason"] == "ready" for row in rows),
                "missing": max(count - len(found), 0), "extra": max(len(found) - count, 0), "replicas": rows}
        if services is None:
            report["routes"]["gateway"] = self.gateway_ready(containers)
            if not report["routes"]["gateway"]:
                report["failures"].append("Nginx: локальный маршрут не готов")
        if "ner-triton" in wanted:
            report["routes"]["triton"] = self.triton_route_ready(containers)
            if not report["routes"]["triton"]:
                report["failures"].append("Nginx: внутренний маршрут к Triton не готов")
        pools = {"presidio-analyzer", "ner-triton"} & set(wanted)
        core_ready = all(row["ready"] == row["expected"] == row["found"]
                         for service, row in report["services"].items() if service not in pools)
        capacity = bool(pools) and all(report["services"][service]["ready"] > 0 for service in pools)
        report["status"] = ("full" if not report["failures"] else
                            "degraded" if core_ready and capacity and all(report["routes"].values()) else "unavailable")
        report["model_identity"] = dict(self._identity) if self._identity is not None else None
        return report

    @staticmethod
    def print_readiness(report):
        print("# readiness " + " ".join(f"{key}={report[key]}" for key in ("scope", "role", "project", "remote_node", "configured_bundle", "status")), flush=True)
        if report["model_identity"] is not None:
            print("# trusted_model " + json.dumps(report["model_identity"], sort_keys=True), flush=True)
        for service, row in report["services"].items():
            print(f"{service}: expected={row['expected']} found={row['found']} ready={row['ready']} missing={row['missing']} extra={row['extra']}", flush=True)
            for replica in row["replicas"]:
                state = "healthy" if replica["reason"] == "ready" else replica["reason"]
                print(f"{replica['name']}: {state}", flush=True)
        for route, ready in report["routes"].items():
            print(f"route/{route}: {'ready' if ready else 'unavailable'}", flush=True)

    def gateway_ready(self, containers):
        ingress = next((item for item in containers if item["Config"]["Labels"].get("com.docker.compose.service") == "nginx"
                        and item["State"].get("Running")), None)
        if ingress is None:
            return False
        paths = (["http://127.0.0.1/nginx-health", "http://127.0.0.1:5001/api/v1/health"]
                 if self.node_role == "compute" else ["http://127.0.0.1/health/liveliness"])
        if "codex-lb" in self.counts:
            paths.append("http://127.0.0.1:2455/health/ready")
        for url in paths:
            try:
                result = subprocess.run(["docker", "exec", ingress["Id"], "wget", "-qO-", "-T", "5", url], capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                return False
            if result.returncode:
                return False
        return True

    def triton_route_ready(self, containers):
        ingress = next((item for item in containers
                        if item["Config"]["Labels"].get("com.docker.compose.service") == "nginx"
                        and item["State"].get("Running")), None)
        if ingress is None:
            return False
        try:
            result = subprocess.run(["docker", "exec", ingress["Id"], "wget", "-qO-", "-T", "5",
                "http://127.0.0.1:8000/v2/models/tiny2/versions/1/ready"], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def ner_readiness(self, item):
        code = "import json, urllib.request; client=urllib.request.build_opener(urllib.request.ProxyHandler({})); data=client.open('http://127.0.0.1:5001/api/v1/health', timeout=5).read(65537); assert len(data)<=65536; print(data.decode())"
        try:
            result = subprocess.run(["docker", "exec", item["Id"], "python", "-c", code], text=True, capture_output=True, timeout=10)
            if result.returncode:
                return "ner_probe_failed"
            data = json.loads(result.stdout)
            if not isinstance(data, dict):
                return "ner_invalid_response"
            if not (data.get("ner_model") == self.model_identity["model_id"]
                    and data.get("ner_revision") == self.model_identity["revision"]
                    and data.get("ner_onnx_sha256") == self.model_identity["onnx_sha256"]):
                return "identity_mismatch"
            return "ready" if data.get("ner_state") == "ready" and data.get("ner_warmed_up") is True else "not_warmed_up"
        except ValueError:
            return "ner_invalid_response"
        except (OSError, DeploymentError, subprocess.SubprocessError):
            return "ner_probe_failed"

    @property
    def model_identity(self):
        if self._identity is None:
            # Read the trusted metadata from the target build, never from the
            # first response of a balanced server or a manually supplied hash.
            image = self.model["services"]["ner-triton"]["image"]
            output = subprocess.check_output(["docker", "run", "--rm", "--network", "none",
                "--entrypoint", "python3", image, "/opt/ru-ner/verify_triton_service.py", "--identity-only"],
                text=True, stderr=subprocess.DEVNULL, timeout=30)
            identity = json.loads(output)
            if (not isinstance(identity, dict) or
                    set(identity) != {"model_id", "revision", "onnx_sha256"} or
                    any(not isinstance(identity[key], str) or not identity[key] for key in ("model_id", "revision")) or
                    not isinstance(identity["onnx_sha256"], str) or
                    not re.fullmatch(r"[0-9a-f]{64}", identity["onnx_sha256"])):
                raise DeploymentError("Не удалось прочитать доверенную идентичность сборки Triton")
            self._identity = identity
        return self._identity

    def triton_readiness(self, item):
        try:
            result = subprocess.run(["docker", "exec", item["Id"], "python3",
                "/opt/ru-ner/verify_triton_service.py"], text=True, capture_output=True, timeout=10)
            if result.returncode:
                return "triton_probe_failed"
            return "ready" if json.loads(result.stdout) == self.model_identity else "identity_mismatch"
        except ValueError:
            return "triton_invalid_response"
        except (OSError, DeploymentError, subprocess.SubprocessError):
            return "triton_probe_failed"

    def start(self, force=False, only_litellm=False):
        if only_litellm:
            self.require_primary()
        self.guard("preflight")
        self.guard("mutation" if only_litellm else "switch")
        self.prepare_runtime()
        self.model
        self.summary()
        previous = self.containers()
        serving = list(previous)
        timeout = self.start_timeout
        if not force:
            for service in ("litellm", "presidio-analyzer", "ner-triton"):
                if service not in self.counts:
                    continue
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
        for service in ("presidio-analyzer", "ner-triton"):
            if any(item["State"]["Running"] and
                   item["Config"]["Labels"].get("com.docker.compose.service") == service and
                   item["Config"]["Labels"].get("ru-llm-proxy.topology", "functional") != self.topology
                   for item in serving):
                # Functional Analyzer publishes 5001; production Nginx takes
                # that port. Release it before bringing the new gateway up.
                self.compose("stop", service)
                previous = [item for item in previous
                            if item["Config"]["Labels"].get("com.docker.compose.service") != service]
        self.persist_mode()
        if force:
            previous = []
        flags = ["--force-recreate"] if force else []
        # Full configuration from the start; the operator isolates real client
        # traffic during deployment. Nginx must precede the required Analyzer.
        self.compose("up", "-d", "--no-build", "--no-deps", *flags, "nginx")
        self.compose("exec", "-T", "nginx", "nginx", "-t")
        self.health(timeout, {"nginx": 1})
        if not only_litellm:
            if "db" in self.counts:
                self.compose("up", "-d", "--no-build", *flags, "db", "redis")
                self.health(timeout, {"db": 1, "redis": 1})
            for count in self.start_counts(previous, "ner-triton"):
                self.compose("up", "-d", "--no-build", "--no-deps", *flags,
                             "--scale", f"ner-triton={count}", "ner-triton")
                self.health(timeout, {"ner-triton": count})
                flags = []
        flags = ["--force-recreate"] if force else []
        # Sequential cold starts keep checkpoint-loading peaks bounded.
        for count in ([] if only_litellm else self.start_counts(previous, "presidio-analyzer")):
            self.compose("up", "-d", "--no-build", "--no-deps", *flags, "--scale", f"presidio-analyzer={count}", "presidio-analyzer")
            self.health(timeout, {"presidio-analyzer": count})
            flags = []
        if "codex-lb" in self.counts and not only_litellm:
            self.compose("up", "-d", "--no-build", *(["--force-recreate"] if force else []), "codex-lb-db", "codex-lb")
            self.health(timeout, {"codex-lb-db": 1, "codex-lb": 1})
        flags = ["--force-recreate"] if force else []
        for count in (self.start_counts(previous, "litellm") if "litellm" in self.counts else []):
            self.compose("up", "-d", "--no-build", "--no-deps", *flags, "--scale", f"litellm={count}", "litellm")
            self.health(timeout, {"litellm": count})
            flags = []
        self.health(timeout)

    def start_counts(self, previous, service):
        current = sum(item["State"]["Running"] for item in previous if item["Config"]["Labels"].get("com.docker.compose.service") == service)
        desired = self.counts[service]
        return range(max(1, current), desired + 1) if current < desired else [desired]

    def url(self):
        self.require_primary()
        if os.getenv("LITELLM_URL"):
            return os.environ["LITELLM_URL"].rstrip("/")
        port = os.environ["LITELLM_PORT"] if "LITELLM_PORT" in os.environ else setting(self.env_file, "LITELLM_PORT", "4000")
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise DeploymentError("Некорректный LITELLM_PORT")
        return f"http://localhost:{port}"

    def metrics(self):
        """Keep each reachable replica's snapshot even when its peers fail."""
        self.guard("mutation")
        containers = self.containers()
        print("# metrics " + " ".join(f"{key}={value}" for key, value in self.diagnostic_scope().items()), flush=True)
        failures = []
        for service, expected in self.counts.items():
            if service not in {"litellm", "presidio-analyzer", "ner-triton"}:
                continue
            found = sorted((item for item in containers if item["Config"]["Labels"].get("com.docker.compose.service") == service),
                           key=lambda item: item["Name"])
            if len(found) != expected:
                failures.append(f"{service}: ожидается {expected}, найдено {len(found)}")
            port = {"litellm": 4000, "presidio-analyzer": 5001, "ner-triton": 8002}[service]
            for item in found:
                name = item["Name"].lstrip("/")
                reason = None
                if not item["State"]["Running"]:
                    reason = "stopped"
                else:
                    code = (f"import urllib.request; client=urllib.request.build_opener(urllib.request.ProxyHandler({{}})); "
                            f"data=client.open('http://127.0.0.1:{port}/metrics', timeout=10).read(8388609); "
                            "assert len(data)<=8388608; print(data.decode())")
                    try:
                        result = subprocess.check_output(["docker", "exec", item["Id"], "python3", "-c", code],
                                                         text=True, stderr=subprocess.DEVNULL, timeout=15)
                        if not any(line.startswith(("litellm_", "ru_pii_guardrail_", "ru_presidio_", "nv_inference_")) for line in result.splitlines()):
                            reason = "metrics_missing"
                    except subprocess.TimeoutExpired:
                        reason = "metrics_timeout"
                    except (OSError, subprocess.SubprocessError):
                        reason = "metrics_unavailable"
                print(f"# service={service} replica={name} /metrics status={reason or 'collected'}", flush=True)
                if reason:
                    failures.append(f"{name}: {reason}")
                else:
                    print(result, flush=True)
        if failures:
            raise DeploymentError("Неполный сбор метрик: " + "; ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack")
    parser.add_argument("--env-file")
    parser.add_argument("action", choices=["validate", "build", "setup", "start", "guard", "health", "compose", "url", "metrics", "exec"])
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        context = Context(stack=args.stack, env_file=args.env_file)
        if args.action == "validate":
            context.validate_operations(args.args)
            return
        if args.action == "build":
            context.build()
        elif args.action == "setup":
            context.setup()
        elif args.action == "start":
            context.start(force="--force-recreate" in args.args, only_litellm="--only-litellm" in args.args)
        elif args.action == "guard":
            context.load_runtime()
            context.guard(args.args[0])
        elif args.action == "health":
            context.load_runtime()
            context.guard("mutation")
            context.summary()
            context.health(int(args.args[0]) if args.args else 0)
        elif args.action == "compose":
            if args.args and args.args[0] == "config" and tuple(args.args[1:]) in READ_ONLY_CONFIG:
                if context.node_role is not None:
                    context.node_config
                    context.load_runtime(validate_current=True)
            else:
                if context.node_role is not None:
                    context.validate_operations(args.args)
                    if args.args and args.args[0] in {"up", "start", "restart", "create", "run"}:
                        context.guard("mutation")
                        context.prepare_runtime()
                    else:
                        context.load_runtime()
                    context.guard("mutation")
                else:
                    context.guard_role(context.containers())
            context.compose(*args.args)
        elif args.action == "url":
            print(context.url())
        elif args.action == "metrics":
            context.load_runtime()
            context.metrics()
        elif args.action == "exec":
            context.require_primary()
            context.load_runtime()
            context.guard("mutation")
            environment = dict(context.environment)
            files = context.files + ([str(context._bundle.overlay)] if context._bundle is not None else [])
            environment.update(ENV_FILE=str(context.env_file), LITELLM_URL=os.getenv("LITELLM_URL", context.url()), COMPOSE_FILE=os.pathsep.join(files), COMPOSE_ENV_FILES=str(context.env_file))
            subprocess.run(args.args, cwd=ROOT, env=environment, check=True)
    except (DeploymentError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"Ошибка развёртывания: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
