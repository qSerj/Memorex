from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
class PacketUpload:
    name: str
    mime_type: str | None
    data: bytes
    processing_mode: Literal["analyze", "store"] = "analyze"
    analysis_instruction: str = ""


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

    def run(
        self,
        workdir: Path,
        prompt: str,
        *,
        writable: bool,
        images: list[Path] | None = None,
    ) -> RunnerResult:
        raise NotImplementedError

    def run_with_progress(
        self,
        workdir: Path,
        prompt: str,
        *,
        writable: bool,
        images: list[Path] | None = None,
        progress: Callable[[dict[str, object]], None] | None = None,
        cancel_event: Event | None = None,
    ) -> RunnerResult:
        """Compatibility hook for streaming runners and simple test doubles."""
        return self.run(workdir, prompt, writable=writable)


class ProposalAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["upsert", "delete", "rename"]
    path: str
    destination: str | None = None


class ProposalActions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actions: list[ProposalAction] = Field(default_factory=list)
