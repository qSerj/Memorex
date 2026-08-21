from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from memorex.config import WikiSettings
from memorex.wiki_first.models import AgentRunner, RunnerResult


class RunnerError(RuntimeError):
    def __init__(self, message: str, *, result: RunnerResult | None = None):
        super().__init__(message)
        self.result = result


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="allow")

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


class TelemetryEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    usage: TokenUsage | None = None


class CLIAgentRunner(AgentRunner):
    def __init__(self, name: str, model: str, effort: str, *, timeout: int = 1800):
        if name not in {"claude", "codex"}:
            raise ValueError(f"Unsupported Wiki runner: {name}")
        self.name = name
        self.model = model
        self.effort = effort
        self.timeout = timeout

    def run(self, workdir: Path, prompt: str, *, writable: bool) -> RunnerResult:
        executable = shutil.which(self.name)
        if executable is None:
            raise RunnerError(f"Runner executable is not installed: {self.name}")
        command = self._command(executable, prompt, writable=writable)
        version = self._version(executable)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = int((time.monotonic() - started) * 1000)
            result = RunnerResult(
                runner=self.name,
                model=self.model,
                command_version=version,
                duration_ms=duration,
                stdout=str(exc.stdout or ""),
                stderr=str(exc.stderr or ""),
            )
            raise RunnerError(
                f"{self.name} timed out after {self.timeout} seconds", result=result
            ) from exc
        duration = int((time.monotonic() - started) * 1000)
        result = RunnerResult(
            runner=self.name,
            model=self.model,
            command_version=version,
            duration_ms=duration,
            stdout=completed.stdout,
            stderr=completed.stderr,
            **_parse_usage(self.name, completed.stdout),
        )
        if completed.returncode != 0:
            raise RunnerError(
                f"{self.name} exited with code {completed.returncode}: {completed.stderr.strip()}",
                result=result,
            )
        return result

    def _command(self, executable: str, prompt: str, *, writable: bool) -> list[str]:
        if self.name == "claude":
            tools = "Read,Glob,Grep" if not writable else "Read,Write,Edit,Glob,Grep"
            return [
                executable,
                "--model",
                self.model,
                "--effort",
                self.effort,
                "--permission-mode",
                "acceptEdits",
                "--tools",
                tools,
                "--output-format",
                "json",
                "-p",
                "--",
                prompt,
            ]
        sandbox = "workspace-write" if writable else "read-only"
        return [
            executable,
            "exec",
            "--model",
            self.model,
            "-c",
            f'model_reasoning_effort="{self.effort}"',
            "--sandbox",
            sandbox,
            "--skip-git-repo-check",
            "--json",
            prompt,
        ]

    def _version(self, executable: str) -> str | None:
        try:
            result = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return (result.stdout or result.stderr).strip() or None


def configured_runner(settings: WikiSettings, name: str) -> CLIAgentRunner:
    if name == "claude":
        return CLIAgentRunner(name, settings.claude_model, settings.claude_effort)
    if name == "codex":
        return CLIAgentRunner(name, settings.codex_model, settings.codex_reasoning_effort)
    raise ValueError(f"Unsupported Wiki runner: {name}")


def _parse_usage(runner: str, stdout: str) -> dict[str, int | None]:
    envelopes: list[TelemetryEnvelope] = []
    candidates = [stdout] if runner == "claude" else stdout.splitlines()
    for candidate in candidates:
        try:
            envelopes.append(TelemetryEnvelope.model_validate_json(candidate))
        except ValidationError:
            continue
    usage = next((item.usage for item in reversed(envelopes) if item.usage is not None), None)
    if usage is None:
        return {"input_tokens": None, "output_tokens": None, "cached_input_tokens": None}
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": usage.cached_input_tokens or usage.cache_read_input_tokens,
    }
