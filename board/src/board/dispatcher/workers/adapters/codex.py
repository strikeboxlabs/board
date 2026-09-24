from __future__ import annotations

from board.dispatcher.config import WorkerConfig
from board.dispatcher.workers.base import DriverResult, RegexSessionDriver
from board.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


class CodexDriver(RegexSessionDriver):
    type_name = "codex"

    def __init__(self, local: bool = False):
        self.local = local

    def local_binary(self) -> str | None:
        return "codex"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        env = worker.env
        return http_ping(
            f"{env['CODEX_BASE_URL']}/responses",
            headers={
                "Authorization": f"Bearer {env['OPENAI_API_KEY']}",
                "content-type": "application/json",
            },
            json_body={
                "model": env["CODEX_MODEL"],
                "input": [{"role": "user", "content": "ping"}],
                "stream": False,
            },
            timeout=timeout,
            proxies=proxies_from_env(env),
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        return f"POST {worker.env['CODEX_BASE_URL']}/responses (model={worker.env['CODEX_MODEL']})"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        if self.local:
            return DriverResult(
                argv=[
                    "codex",
                    "exec",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--",
                    prompt,
                ]
            )
        env = worker.env
        return DriverResult(
            argv=[
                "codex",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "--model",
                env["CODEX_MODEL"],
                "-c",
                'model_provider="board"',
                "-c",
                'model_providers.board.name="board"',
                "-c",
                'model_providers.board.wire_api="responses"',
                "-c",
                'model_reasoning_effort="high"',
                "-c",
                f'model_providers.board.base_url="{env["CODEX_BASE_URL"]}"',
                "-c",
                'model_providers.board.env_key="OPENAI_API_KEY"',
                "--",
                prompt,
            ]
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        if self.local:
            return [
                "codex",
                "exec",
                "resume",
                session,
                "--dangerously-bypass-approvals-and-sandbox",
                "--",
                prompt,
            ]
        env = worker.env
        return [
            "codex",
            "exec",
            "resume",
            session,
            "--dangerously-bypass-approvals-and-sandbox",
            "--model",
            env["CODEX_MODEL"],
            "-c",
            'model_provider="board"',
            "-c",
            'model_providers.board.name="board"',
            "-c",
            'model_providers.board.wire_api="responses"',
            "-c",
            'model_reasoning_effort="high"',
            "-c",
            f'model_providers.board.base_url="{env["CODEX_BASE_URL"]}"',
            "-c",
            'model_providers.board.env_key="OPENAI_API_KEY"',
            "--",
            prompt,
        ]
