from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from memorex.config import WikiSettings
from memorex.wiki_first.models import AgentRunner, RunnerResult


class RunnerError(RuntimeError):
    def __init__(self, message: str, *, result: RunnerResult | None = None):
        super().__init__(message)
        self.result = result


class RunnerCancelled(RunnerError):
    """Raised after a requested, graceful runner termination."""


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
        return self.run_with_progress(workdir, prompt, writable=writable)

    def run_with_progress(
        self,
        workdir: Path,
        prompt: str,
        *,
        writable: bool,
        progress: Callable[[dict[str, object]], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> RunnerResult:
        executable = shutil.which(self.name)
        if executable is None:
            raise RunnerError(f"Runner executable is not installed: {self.name}")
        command = self._command(executable, prompt, writable=writable)
        version = self._version(executable)
        started = time.monotonic()
        if progress:
            progress({"phase": "runner", "runner": self.name, "model": self.model})
        process = subprocess.Popen(
            command,
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def drain(stream: object, target: list[str], *, events: bool) -> None:
            if stream is None:
                return
            for line in stream:
                target.append(line)
                if events and progress and (event := _safe_progress_event(line)):
                    progress(event)

        readers = [
            threading.Thread(
                target=drain, args=(process.stdout, stdout_lines), kwargs={"events": True}
            ),
            threading.Thread(
                target=drain, args=(process.stderr, stderr_lines), kwargs={"events": False}
            ),
        ]
        for reader in readers:
            reader.start()
        cancelled = False
        timed_out = False
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                break
            if time.monotonic() - started > self.timeout:
                timed_out = True
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                break
            time.sleep(0.05)
        for reader in readers:
            reader.join(timeout=5)
        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)
        duration = int((time.monotonic() - started) * 1000)
        result = RunnerResult(
            runner=self.name,
            model=self.model,
            command_version=version,
            duration_ms=duration,
            stdout=stdout,
            stderr=stderr,
            **_parse_usage(self.name, stdout),
        )
        if cancelled:
            raise RunnerCancelled(f"{self.name} was stopped", result=result)
        if timed_out:
            raise RunnerError(f"{self.name} timed out after {self.timeout} seconds", result=result)
        if process.returncode != 0:
            raise RunnerError(
                f"{self.name} exited with code {process.returncode}: {stderr.strip()}",
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
                "stream-json",
                "--verbose",
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
            process = subprocess.Popen(
                [executable, "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = process.communicate(timeout=10)
        except OSError:
            return None
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return None
        return (stdout or stderr).strip() or None


def configured_runner(settings: WikiSettings, name: str) -> CLIAgentRunner:
    if name == "claude":
        return CLIAgentRunner(name, settings.claude_model, settings.claude_effort)
    if name == "codex":
        return CLIAgentRunner(name, settings.codex_model, settings.codex_reasoning_effort)
    raise ValueError(f"Unsupported Wiki runner: {name}")


def _parse_usage(runner: str, stdout: str) -> dict[str, int | None]:
    envelopes: list[TelemetryEnvelope] = []
    candidates = stdout.splitlines()
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


def _safe_progress_event(line: str) -> dict[str, object] | None:
    """Reduce runner JSONL to operational metadata; never expose model text/reasoning."""
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    event_type = str(payload.get("type") or payload.get("subtype") or "")
    if event_type in {"turn.started", "system", "init"}:
        return {"phase": "model-started"}
    if event_type in {"item.completed", "tool_use", "tool_result"}:
        return {"phase": "model-working"}
    if event_type in {"turn.completed", "result", "success"}:
        return {"phase": "model-completed"}
    return None
