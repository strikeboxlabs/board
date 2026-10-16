from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from board.dispatcher.config import WorkerConfig
from board.dispatcher.workers.base import DriverResult, WorkerDriver
from board.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


class PiDriver(WorkerDriver):
    type_name = "pi"

    def __init__(self, local: bool = False):
        self.local = local

    def local_binary(self) -> str | None:
        return "pi"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        env = worker.env
        base = env["PI_BASE_URL"].rstrip("/")
        model = env["PI_MODEL"]
        api = env["PI_PROVIDER_API"]
        proxies = proxies_from_env(env)
        headers = {"Authorization": f"Bearer {env['PI_API_KEY']}", "content-type": "application/json"}
        if "anthropic" in api:
            return http_ping(
                f"{base}/v1/messages",
                headers={**headers, "anthropic-version": "2023-06-01"},
                json_body={"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": "ping"}]},
                timeout=timeout,
                proxies=proxies,
            )
        if "responses" in api:
            return http_ping(
                f"{base}/responses",
                headers=headers,
                json_body={"model": model, "input": [{"role": "user", "content": "ping"}], "stream": False},
                timeout=timeout,
                proxies=proxies,
            )
        # openai-completions and anything else: OpenAI-compatible chat/completions
        return http_ping(
            f"{base}/chat/completions",
            headers=headers,
            json_body={"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": "ping"}]},
            timeout=timeout,
            proxies=proxies,
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        env = worker.env
        return f"POST {env['PI_BASE_URL']} (api={env['PI_PROVIDER_API']}, model={env['PI_MODEL']})"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        if self.local:
            return DriverResult(argv=self._local_argv(worker, prompt, session), session=session)
        env = worker.env
        argv = [
            "--provider",
            "board",
            "--model",
            env["PI_MODEL"],
            "--mode",
            "json",
            "--session-dir",
            self._session_dir(worker),
        ]
        if session:
            argv.extend(["--session", session])
        argv.extend(["-p", prompt])
        return DriverResult(argv=self._wrap_with_models(worker, argv), session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        if self.local:
            return self._local_argv(worker, prompt, session)
        env = worker.env
        argv = [
            "--provider",
            "board",
            "--model",
            env["PI_MODEL"],
            "--mode",
            "json",
            "--session-dir",
            self._session_dir(worker),
            "--session",
            session,
            "-p",
            prompt,
        ]
        return self._wrap_with_models(worker, argv)

    def _local_argv(self, worker: WorkerConfig, prompt: str, session: str | None) -> list[str]:
        # Local workers retain the daemon user's Pi provider/auth configuration.
        session_dir = worker.env.get("BOARD_PI_SESSION_DIR", self._session_dir(worker))
        pi_argv = ["--mode", "json", "--no-extensions", "--no-skills",
                   "--no-prompt-templates", "--no-themes", "--no-context-files", "--no-approve",
                   "--tools", ",".join(worker.pi_tools)]
        paths = json.loads(worker.env.get("BOARD_PI_SKILLS_JSON", "[]"))
        if not isinstance(paths, list) or any(not isinstance(path, str) or not path for path in paths):
            raise ValueError("Invalid resolved Pi skill paths")
        for path in paths:
            pi_argv.extend(["--skill", path])
        for extension in worker.pi_extensions:
            pi_argv.extend(["--extension", extension])
        if worker.env.get("BOARD_PI_CONTEXT_FILE"):
            pi_argv.extend(["--append-system-prompt", worker.env["BOARD_PI_CONTEXT_FILE"]])
        if session:
            pi_argv.extend(["--session", session])
        pi_argv.extend(["-p", prompt])
        # Exactly one --session-dir, with a restrictive directory creation mask.
        script = 'sdir="$1"\nshift\numask 077\nmkdir -p "$sdir" || exit 1\nexec pi --session-dir "$sdir" "$@"\n'
        return ["/bin/sh", "-lc", script, "--", session_dir, *pi_argv]

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        for event in self._iter_events(stdout):
            if event.get("type") != "session":
                continue
            session_id = event.get("id")
            if isinstance(session_id, str) and session_id:
                return session_id
        return None

    @classmethod
    def response_messages(cls, stdout: str) -> list[dict[str, Any]]:
        # agent_end repeats earlier messages; prefer the complete list to avoid
        # counting streamed/turn_end copies of the same usage more than once.
        turns: list[dict[str, Any]] = []
        final: list[dict[str, Any]] | None = None
        for event in cls._iter_events(stdout):
            if event.get("type") == "turn_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    turns.append(message)
            elif event.get("type") == "agent_end" and isinstance(event.get("messages"), list):
                final = [m for m in event["messages"]
                         if isinstance(m, dict) and m.get("role") == "assistant"]
        return final if final else turns

    @classmethod
    def output_diagnostics(cls, stdout: str) -> dict[str, Any]:
        messages = cls.response_messages(stdout)
        usage = {key: 0 for key in ("input", "output", "cacheRead", "cacheWrite", "reasoning", "totalTokens")}
        for message in messages:
            for key in usage:
                value = message.get("usage", {}).get(key, 0)
                if isinstance(value, (int, float)):
                    usage[key] += value
        return {"usage": usage, "usage_available": bool(messages and any(m.get("usage") for m in messages)),
                "stop_reason": messages[-1].get("stopReason") if messages else None,
                "model": messages[-1].get("model") if messages else None,
                "model_calls": len(messages)}

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        messages = self.response_messages(stdout)
        if not messages:
            raise ValueError("pi_missing_assistant_response")
        message = messages[-1]
        stop = message.get("stopReason")
        if stop == "length":
            raise ValueError("pi_output_limit: final answer exhausted the output budget")
        if stop in ("error", "aborted"):
            raise ValueError(f"pi_response_{stop}")
        content = message.get("content", [])
        text = "\n".join(item.get("text", "") for item in content
                         if isinstance(item, dict) and item.get("type") == "text").strip() if isinstance(content, list) else ""
        if not text:
            raise ValueError("pi_empty_final_answer")
        return text

    def build_reason(self, worker: WorkerConfig, prompt_path: str, extension_path: str) -> DriverResult:
        command = self.build_execute(worker, "@" + prompt_path, None)
        argv = command.argv
        # Explicit extensions still load with --no-extensions. Limit the planner
        # to the supplied graph; exploration belongs in explore tasks.
        index = argv.index("--tools")
        argv[index:index + 2] = ["--no-tools", "--extension", extension_path]
        return command

    def _wrap_with_models(self, worker: WorkerConfig, pi_argv: list[str], *, enable_tools: bool = True) -> list[str]:
        script = (
            'agent_dir="$1"\n'
            'models_json="$2"\n'
            "shift 2\n"
            'mkdir -p "$agent_dir"\n'
            'mkdir -p "$agent_dir/sessions"\n'
            'printf "%s" "$models_json" > "$agent_dir/models.json"\n'
            'exec env PI_CODING_AGENT_DIR="$agent_dir" pi "$@"\n'
        )
        argv = [
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
        ]
        if enable_tools:
            argv.extend(["--tools", "read,write,edit,bash,grep,find,ls"])
        return [
            "/bin/sh",
            "-lc",
            script,
            "--",
            self._agent_dir(worker),
            self._models_json(worker),
            *argv,
            *pi_argv,
        ]

    @staticmethod
    def _agent_dir(worker: WorkerConfig) -> str:
        # Default /tmp/board-pi is ephemeral by design (pi CLI sessions/models.json scratch);
        # point PI_AGENT_DIR somewhere durable if sessions should survive reboots.
        override = worker.env.get("PI_AGENT_DIR")
        if override:
            return str(PurePosixPath(override) / worker.name)
        return str(PurePosixPath("/tmp/board-pi") / worker.name)

    @staticmethod
    def _session_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath(PiDriver._agent_dir(worker)) / "sessions")

    @staticmethod
    def _iter_events(stdout: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in stdout.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    @staticmethod
    def _models_json(worker: WorkerConfig) -> str:
        env = worker.env
        model: dict[str, Any] = {
            "id": env["PI_MODEL"],
            "name": env["PI_MODEL"],
        }
        context_window = env.get("PI_MODEL_CONTEXT_WINDOW")
        if context_window:
            model["contextWindow"] = int(context_window)

        provider: dict[str, Any] = {
            "baseUrl": env["PI_BASE_URL"],
            "api": env["PI_PROVIDER_API"],
            "apiKey": env["PI_API_KEY"],
            "models": [model],
        }
        payload = {"providers": {"board": provider}}
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
