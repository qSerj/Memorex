from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when required runtime configuration is missing."""


@dataclass(frozen=True)
class WorkspaceConfig:
    data_dir: Path

    @property
    def database_path(self) -> Path:
        return self.data_dir / "memorex.db"

    @property
    def objects_dir(self) -> Path:
        return self.data_dir / "objects"

    @classmethod
    def resolve(cls, data_dir: Path | None = None) -> WorkspaceConfig:
        configured = data_dir or os.getenv("MEMOREX_DATA_DIR") or ".memorex"
        return cls(Path(configured).expanduser().resolve())


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str | None

    @classmethod
    def resolve(cls, base_url: str | None = None, model: str | None = None) -> LLMConfig:
        resolved_base_url = base_url or os.getenv("MEMOREX_LLM_BASE_URL")
        resolved_model = model or os.getenv("MEMOREX_LLM_MODEL")
        missing = [
            name
            for name, value in (
                ("MEMOREX_LLM_BASE_URL/--base-url", resolved_base_url),
                ("MEMOREX_LLM_MODEL/--model", resolved_model),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError(f"Missing LLM configuration: {', '.join(missing)}")
        return cls(
            base_url=resolved_base_url.rstrip("/"),
            model=resolved_model,
            api_key=os.getenv("MEMOREX_LLM_API_KEY"),
        )
