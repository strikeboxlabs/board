"""Exercise pinned Pi against an owned loopback stub, never a real model provider."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

from board.dispatcher.config import DispatchConfig, WorkerConfig
from board.dispatcher.contracts import validate_explore_payload
from board.dispatcher.runtime.local_backend import LocalBackend
from board.dispatcher.skills import prepare_pi_worker
from board.dispatcher.workers.adapters.pi import PiDriver

from conftest import make_config


BODY_MARKER = "OWNED_SELECTED_SKILL_BODY_9f7042"
SELECTED_DESCRIPTION = "OWNED_SELECTED_SKILL_DESCRIPTION_9f7042"
EXCLUDED_MARKER = "EXCLUDED_SKILL_MUST_NOT_BE_ADVERTISED_59a418"
UNTRUSTED_CONTEXT = "WORKSPACE_CONTEXT_MUST_NOT_BE_LOADED_43ca09"


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, snapshot):
        super().__init__(("127.0.0.1", 0), FixtureHandler)
        self.snapshot = str(snapshot / "SKILL.md")
        self.requests = []
        self.errors = []


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.server.errors.append(f"Unexpected local endpoint: {self.path}")
            self.send_error(404)
            return
        if self.headers.get("Authorization") != "Bearer owned-fixture-dummy-key":
            self.server.errors.append("Unexpected dummy authentication")
            self.send_error(401)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length < 2_000_000:
            self.server.errors.append("Unexpected fixture request size")
            self.send_error(413)
            return
        request = json.loads(self.rfile.read(length))
        index = len(self.server.requests)
        self.server.requests.append(request)
        if index > 2:
            self.server.errors.append("Unexpected extra provider request")
            self.send_error(500)
            return
        assert request["model"] == "fixture-model"
        assert request["stream"] is True
        if index == 0:
            delta = {"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call_owned_fixture", "type": "function",
                "function": {"name": "read", "arguments": json.dumps({"path": self.server.snapshot})},
            }]}
            finish = "tool_calls"
        else:
            description = "Owned skill body read and retained." if index == 1 else "Owned session resumed and concluded."
            delta = {"role": "assistant", "content": json.dumps({"description": description})}
            finish = "stop"

        def chunk(choices, **extra):
            return {"id": f"fixture-{index}", "object": "chat.completion.chunk", "created": 1,
                    "model": "fixture-model", "choices": choices, **extra}

        chunks = [
            chunk([{"index": 0, "delta": delta, "finish_reason": None}]),
            chunk([{"index": 0, "delta": {}, "finish_reason": finish}]),
            chunk([], usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}),
        ]
        body = ("".join("data: " + json.dumps(value) + "\n\n" for value in chunks) + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True


def fixture_config(tmp_path):
    library = tmp_path / "library"
    records = []
    for name, description, marker in (
        ("selected-fixture", SELECTED_DESCRIPTION, BODY_MARKER),
        ("excluded-fixture", EXCLUDED_MARKER, EXCLUDED_MARKER),
    ):
        directory = library / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n"
            f"Read this entire harmless fixture. Its marker is {marker}.\n",
            encoding="utf-8")
        records.append({"id": name, "path": f"skills/{name}",
                        "tags": ["execution:linux", "task:data-inspection"], "requires": []})
    catalog = library / "catalog.json"
    catalog.write_text(json.dumps({"schema_version": 1, "library_id": "owned-fixture", "skills": records}), encoding="utf-8")
    worker = WorkerConfig(name="owned-pi", type="pi", task_types=["explore"], max_running=1,
                          priority=0, pi_tools=["read"])
    payload = make_config().model_dump()
    payload["runtime"]["execution"] = "local"
    payload["container"] = None
    payload["local"] = {"evidence_root": str(tmp_path / "projects"), "pi_session_storage": "project"}
    payload["workers"] = [worker.model_dump()]
    payload["skills"] = {"enabled": True, "catalogs": [str(catalog)],
        "default_profiles": ["runtime-fixture"],
        "profiles": {"runtime-fixture": {"skills": ["owned-fixture/selected-fixture"]}}}
    config = DispatchConfig.model_validate(payload)
    backend = LocalBackend(config.local)
    backend.ensure_running("owned-project")
    prepared = prepare_pi_worker(config, config.workers[0], backend, "owned-project", "owned-intent")
    return library, backend, prepared


def isolated_environment(tmp_path, cli, server_port, prepared):
    node = shutil.which("node")
    assert node, "Node >=22.19 must be installed for the pinned Pi runtime test"
    home = tmp_path / "home"
    agent = tmp_path / "agent"
    shims = tmp_path / "bin"
    for directory in (home, agent, shims):
        directory.mkdir()
    # /bin/sh -lc may rebuild PATH; the owned HOME keeps this local shim visible.
    (home / ".profile").write_text('export PATH="$BOARD_TEST_PI_SHIM_DIR:$PATH"\n', encoding="utf-8")
    shim = shims / "pi"
    shim.write_text('#!/bin/sh\nexec "$BOARD_TEST_NODE" "$BOARD_TEST_PI_CLI" "$@"\n', encoding="utf-8")
    shim.chmod(0o700)
    models = {"providers": {"owned-local": {
        "baseUrl": f"http://127.0.0.1:{server_port}/v1", "api": "openai-completions",
        "apiKey": "owned-fixture-dummy-key", "models": [{"id": "fixture-model",
            "name": "Owned dummy model", "reasoning": False, "input": ["text"],
            "contextWindow": 131072, "maxTokens": 1024,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}],
    }}}
    (agent / "models.json").write_text(json.dumps(models), encoding="utf-8")
    (agent / "settings.json").write_text(json.dumps({
        "defaultProvider": "owned-local", "defaultModel": "fixture-model", "defaultThinkingLevel": "off",
        "enableInstallTelemetry": False, "enableAnalytics": False, "cacheWarming": "off",
        "retry": {"enabled": False, "provider": {"maxRetries": 0, "timeoutMs": 10000}},
        "compaction": {"enabled": False},
        # --no-skills must suppress this configured but unselected package.
        "skills": [str(tmp_path / "library" / "skills" / "excluded-fixture")],
    }), encoding="utf-8")
    (agent / "AGENTS.md").write_text(UNTRUSTED_CONTEXT, encoding="utf-8")

    # Enforce loopback-only connections in the Node child, including fetch/undici.
    # A blocked attempt is recorded even if Pi catches its connection error.
    guard = tmp_path / "loopback-only.cjs"
    guard.write_text("""
const fs = require('node:fs');
const net = require('node:net');
const dns = require('node:dns');
function allow(host) {
  if (host === undefined || host === 'localhost' || host === '127.0.0.1' || host === '::1') return;
  fs.appendFileSync(process.env.BOARD_TEST_NETWORK_LOG, String(host) + '\\n');
  throw new Error('Fixture blocks non-loopback connection: ' + host);
}
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function(...args) {
  const first = Array.isArray(args[0]) ? args[0][0] : args[0];
  const host = typeof first === 'object' && first !== null ? first.host : typeof args[1] === 'string' ? args[1] : undefined;
  allow(host);
  return Reflect.apply(connect, this, args);
};
const lookup = dns.lookup;
dns.lookup = function(host, ...args) { allow(host); return Reflect.apply(lookup, this, [host, ...args]); };
""", encoding="utf-8")
    # Intentionally do not inherit provider credentials, proxies, NODE_OPTIONS or user settings.
    return {"PATH": str(shims) + os.pathsep + os.environ.get("PATH", ""), "HOME": str(home),
        "LANG": "C.UTF-8", "CI": "1", "NO_COLOR": "1", "PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1",
        "PI_CODING_AGENT_DIR": str(agent), "BOARD_TEST_PI_CLI": str(cli), "BOARD_TEST_NODE": node,
        "BOARD_TEST_PI_SHIM_DIR": str(shims), "BOARD_TEST_NETWORK_LOG": str(tmp_path / "blocked-network.log"),
        "NODE_OPTIONS": "--require=" + json.dumps(str(guard)), **prepared.env}


def run_pi(argv, workspace, environment):
    result = subprocess.run(argv, cwd=workspace, env=environment, capture_output=True,
                            text=True, timeout=45)
    assert result.returncode == 0, f"Pi exit {result.returncode}:\n{result.stdout[-12000:]}\n{result.stderr[-12000:]}"
    return result


def test_actual_pi_13_loads_selected_snapshot_and_resumes_board_session(tmp_path):
    configured = os.environ.get("BOARD_TEST_PI_CLI")
    if not configured and os.name == "nt":
        pytest.skip("Pinned Pi runtime integration runs in Linux CI; BOARD_TEST_PI_CLI is absent on this Windows host")
    assert configured, "Linux full-suite CI must set BOARD_TEST_PI_CLI to the pinned Pi1.3 bundle"
    cli = Path(configured).resolve(strict=True)
    assert json.loads((cli.parents[2] / "package.json").read_text())["version"] == "1.3.0"
    library, backend, prepared = fixture_config(tmp_path)
    snapshots = json.loads(prepared.env["BOARD_PI_SKILLS_JSON"])
    assert len(snapshots) == 1
    snapshot = Path(snapshots[0])
    workspace = backend.project_dir("owned-project") / "workspace"
    (workspace / "AGENTS.md").write_text(UNTRUSTED_CONTEXT, encoding="utf-8")
    server = FixtureServer(snapshot)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        environment = isolated_environment(tmp_path, cli, server.server_port, prepared)
        driver = PiDriver(local=True)
        execute = driver.build_execute(prepared, "Read the selected harmless skill and report a Board fact.", None)
        first = run_pi(execute.argv, workspace, environment)
        session = driver.extract_session(None, first.stdout, first.stderr)
        assert session
        first_payload = json.loads(driver.extract_response_text(first.stdout, first.stderr))
        assert validate_explore_payload(first_payload)[:2] == ("fact", "Owned skill body read and retained.")
        assert len(server.requests) == 2
        session_dir = Path(prepared.env["BOARD_PI_SESSION_DIR"])
        sessions = list(session_dir.rglob("*.jsonl"))
        assert len(sessions) == 1
        session_path = sessions[0]
        original_size = session_path.stat().st_size

        # A later source edit must not alter the already selected frozen package.
        (library / "skills" / "selected-fixture" / "SKILL.md").write_text("Changed after preparation.\n", encoding="utf-8")
        conclude = driver.build_conclude(prepared, "Conclude using only the existing local fixture evidence.", session)
        assert [execute.argv[i + 1] for i, arg in enumerate(execute.argv[:-1]) if arg == "--skill"] == snapshots
        assert [conclude[i + 1] for i, arg in enumerate(conclude[:-1]) if arg == "--skill"] == snapshots
        second = run_pi(conclude, workspace, environment)
        resumed_header = next(event for event in driver._iter_events(second.stdout) if event.get("type") == "session")
        assert resumed_header["id"] == session
        second_payload = json.loads(driver.extract_response_text(second.stdout, second.stderr))
        assert validate_explore_payload(second_payload)[:2] == ("fact", "Owned session resumed and concluded.")
        assert list(session_dir.rglob("*.jsonl")) == [session_path]
        assert session_path.stat().st_size > original_size
        assert BODY_MARKER in (snapshot / "SKILL.md").read_text(encoding="utf-8")
        assert len(server.requests) == 3
        assert server.errors == []
        initial = json.dumps(server.requests[0]["messages"])
        assert SELECTED_DESCRIPTION in initial
        assert BODY_MARKER not in initial
        assert "# Board worker contract" in initial
        assert "Selected skill profiles: runtime-fixture." in initial
        for request in server.requests:
            transcript = json.dumps(request["messages"])
            assert EXCLUDED_MARKER not in transcript
            assert UNTRUSTED_CONTEXT not in transcript
        tool_results = [message for message in server.requests[1]["messages"] if message["role"] == "tool"]
        assert tool_results and BODY_MARKER in json.dumps(tool_results)
        assert BODY_MARKER in json.dumps(server.requests[2]["messages"])
        assert not Path(environment["BOARD_TEST_NETWORK_LOG"]).exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
