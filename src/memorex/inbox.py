from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from memorex.compiler import compile_source
from memorex.config import LLMConfig, WorkspaceSettings
from memorex.ingest import SUPPORTED_SUFFIXES, parse_source
from memorex.llm import OpenAICompatibleProvider
from memorex.storage import Storage


def scan_inbox(settings: WorkspaceSettings, storage: Storage) -> list[dict[str, Any]]:
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(settings.inbox_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        data = path.read_bytes()
        stat = path.stat()
        storage.stage_inbox_file(
            path,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            mtime_ns=stat.st_mtime_ns,
        )
    return storage.list_inbox_entries()


def compile_inbox_entry(
    settings: WorkspaceSettings,
    storage: Storage,
    entry_id: int,
    *,
    force: bool = False,
) -> dict[str, Any]:
    entry = storage.get_inbox_entry(entry_id)
    if entry["status"] not in {"ready", "failed", "succeeded"}:
        raise ValueError(f"Inbox entry {entry_id} requires metadata before compilation")
    if not entry["source_kind"] or not entry["authority"] or not entry["title"]:
        raise ValueError(f"Inbox entry {entry_id} requires metadata before compilation")
    storage.mark_inbox_status(entry_id, "processing", error=None)
    try:
        parsed = parse_source(Path(entry["canonical_path"]))
        ingested = storage.ingest_source(parsed)
        storage.set_source_metadata(ingested["version_id"], entry)
        fast_config = LLMConfig.resolve_role("fast", settings)
        strong_config = LLMConfig.resolve_role("strong", settings)
        result = compile_source(
            storage,
            ingested["source_id"],
            OpenAICompatibleProvider(fast_config),
            fast_config,
            OpenAICompatibleProvider(strong_config),
            strong_config,
            force=force,
        )
    except Exception as exc:
        storage.mark_inbox_status(entry_id, "failed", error=str(exc))
        raise
    storage.mark_inbox_status(entry_id, "succeeded", source_id=ingested["source_id"])
    return {"inbox_entry_id": entry_id, "ingest": ingested, "compilation": result}
