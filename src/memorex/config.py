from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


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
class WorkspaceSettings:
    root: Path
    name: str
    language: str
    fast_model: str | None
    strong_model: str | None
    answer_model: str | None

    @property
    def data(self) -> WorkspaceConfig:
        return WorkspaceConfig(self.root / ".memorex")

    @property
    def inbox_dir(self) -> Path:
        return self.root / "inbox"

    @property
    def config_path(self) -> Path:
        return self.root / "memorex.toml"

    @classmethod
    def create(cls, root: Path, name: str, *, language: str = "ru") -> WorkspaceSettings:
        resolved = root.expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        (resolved / "inbox").mkdir(exist_ok=True)
        config_path = resolved / "memorex.toml"
        if not config_path.exists():
            config_path.write_text(
                f"name = {json.dumps(name, ensure_ascii=False)}\n"
                f"language = {json.dumps(language, ensure_ascii=False)}\n\n"
                "[models]\n"
                'fast = ""\n'
                'strong = ""\n'
                'answer = ""\n',
                encoding="utf-8",
            )
        return cls.load(resolved)

    def set_models(self, *, fast: str, strong: str, answer: str) -> WorkspaceSettings:
        self.config_path.write_text(
            f"name = {json.dumps(self.name, ensure_ascii=False)}\n"
            f"language = {json.dumps(self.language, ensure_ascii=False)}\n\n"
            "[models]\n"
            f"fast = {json.dumps(fast.strip(), ensure_ascii=False)}\n"
            f"strong = {json.dumps(strong.strip(), ensure_ascii=False)}\n"
            f"answer = {json.dumps(answer.strip(), ensure_ascii=False)}\n",
            encoding="utf-8",
        )
        return self.load(self.root)

    @classmethod
    def load(cls, root: Path) -> WorkspaceSettings:
        resolved = root.expanduser().resolve()
        config_path = resolved / "memorex.toml"
        if not config_path.is_file():
            raise ConfigurationError(
                f"Not a Memorex workspace: {resolved}; run 'memorex workspace init PATH'"
            )
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        models = raw.get("models", {})
        return cls(
            root=resolved,
            name=str(raw.get("name") or resolved.name),
            language=str(raw.get("language") or "ru"),
            fast_model=str(models.get("fast") or "") or None,
            strong_model=str(models.get("strong") or "") or None,
            answer_model=str(models.get("answer") or "") or None,
        )


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

    @classmethod
    def resolve_role(
        cls,
        role: Literal["fast", "strong", "answer"],
        settings: WorkspaceSettings,
    ) -> LLMConfig:
        configured = {
            "fast": settings.fast_model,
            "strong": settings.strong_model,
            "answer": settings.answer_model,
        }[role]
        role_env = os.getenv(f"MEMOREX_LLM_{role.upper()}_MODEL")
        fallback = os.getenv("MEMOREX_LLM_MODEL")
        model = role_env or configured or fallback
        return cls.resolve(model=model)
