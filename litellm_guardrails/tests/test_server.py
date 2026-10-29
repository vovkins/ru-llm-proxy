"""The entry module extends the public app and delegates CLI ownership."""

from pathlib import Path
import json
import os
import runpy
import selectors
import shlex
import signal
import subprocess
import sys
from types import ModuleType
from unittest.mock import Mock

import pytest
import litellm

from litellm_guardrails.request_cancellation import RequestCancellationMiddleware


@pytest.mark.parametrize("run_name,invoked", [("__main__", True), ("__mp_main__", False)])
def test_native_cli_and_spawned_workers(monkeypatch, run_name, invoked):
    server, cli = ModuleType("litellm.proxy.proxy_server"), ModuleType("litellm.proxy.proxy_cli")
    server.app = Mock()
    cli.run_server = Mock()
    monkeypatch.setattr(litellm, "run_server", cli.run_server)
    monkeypatch.setitem(sys.modules, server.__name__, server)
    monkeypatch.setitem(sys.modules, cli.__name__, cli)
    argv = ["litellm", "--config", "/app/config.yaml", "--num_workers", "2"]
    monkeypatch.setattr(sys, "argv", argv)
    runpy.run_module("litellm_guardrails.server", run_name=run_name)
    server.app.add_middleware.assert_called_once_with(RequestCancellationMiddleware)
    assert cli.run_server.call_count == int(invoked)
    assert sys.argv == argv


@pytest.fixture
def start_entrypoint(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json,os,signal,sys\n"
        "print(json.dumps({'argv':sys.argv[1:], 'pid':os.getpid(), "
        "'openai_enabled':os.getenv('DD_TRACE_OPENAI_ENABLED'), "
        "'traced':os.getenv('ENTRYPOINT_TEST_TRACED')}), flush=True)\n"
        "if os.getenv('ENTRYPOINT_TEST_WAIT') == '1': signal.pause()\n"
        "sys.exit(37)\n"
    )
    runners = {
        "python": "exec " + shlex.quote(sys.executable) + " " + shlex.quote(str(probe)) + ' "$@"\n',
        "ddtrace-run": 'export ENTRYPOINT_TEST_TRACED=1\nexec "$@"\n',
    }
    for name, body in runners.items():
        script = tmp_path / name
        script.write_text("#!/bin/sh\nset -eu\n" + body)
        script.chmod(0o755)
    processes = []

    def start(tracing, openai_enabled=None, *, wait=False):
        env = os.environ.copy()
        for name in ("USE_DDTRACE", "DD_TRACE_OPENAI_ENABLED", "ENTRYPOINT_TEST_TRACED", "ENTRYPOINT_TEST_WAIT"):
            env.pop(name, None)
        env["PATH"] = str(tmp_path) + os.pathsep + env.get("PATH", "")
        if tracing is not None:
            env["USE_DDTRACE"] = tracing
        if openai_enabled is not None:
            env["DD_TRACE_OPENAI_ENABLED"] = openai_enabled
        if wait:
            env["ENTRYPOINT_TEST_WAIT"] = "1"
        entrypoint = Path(__file__).parents[1] / "entrypoint.sh"
        process = subprocess.Popen(
            ["sh", str(entrypoint), "--config", "/config with spaces/config.yaml", "--port", "4000"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        processes.append(process)
        return process

    yield start
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.parametrize("tracing", [None, "false", "true"])
@pytest.mark.parametrize("openai_enabled", [None, "True", "False"])
def test_entrypoint_executes_correct_branch_and_preserves_arguments(start_entrypoint, tracing, openai_enabled):
    process = start_entrypoint(tracing, openai_enabled)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 37, stderr
    assert not stderr
    result = json.loads(stdout)
    assert result["pid"] == process.pid, "The entrypoint must replace itself with exec"
    assert result["argv"] == ["-m", "litellm_guardrails.server", "--config", "/config with spaces/config.yaml", "--port", "4000"]
    assert result["traced"] == ("1" if tracing == "true" else None)
    assert result["openai_enabled"] == ("False" if tracing == "true" else openai_enabled)


@pytest.mark.parametrize("tracing", ["false", "true"])
def test_both_entrypoint_branches_deliver_sigterm_to_application(start_entrypoint, tracing):
    process = start_entrypoint(tracing, wait=True)
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=10), "The application did not start"
        assert json.loads(process.stdout.readline())["pid"] == process.pid
    process.terminate()
    _, stderr = process.communicate(timeout=10)
    assert process.returncode == -signal.SIGTERM, stderr
