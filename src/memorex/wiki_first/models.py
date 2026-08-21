from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RunnerResult:
    runner: str
    model: str
    command_version: str | None
    duration_ms: int
    stdout: str
    stderr: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


@dataclass(frozen=True)
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pages: int = 0
    bytes_total: int = 0

    @property
    def valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "pages": self.pages,
            "bytes": self.bytes_total,
        }


class AgentRunner:
    name: str
    model: str

    def run(self, workdir: Path, prompt: str, *, writable: bool) -> RunnerResult:
        raise NotImplementedError
