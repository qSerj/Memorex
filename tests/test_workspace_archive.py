from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from memorex.config import WorkspaceSettings
from memorex.wiki_first.service import WikiFirstService
from memorex.wiki_first.storage import WikiStorage
from memorex.workspace_archive import (
    WorkspaceArchiveError,
    create_workspace_archive,
    restore_workspace_archive,
)


def test_full_workspace_restore_preserves_previous_workspace_as_backup(tmp_path: Path) -> None:
    source = WorkspaceSettings.create(tmp_path / "source", "Portable memory")
    source_service = WikiFirstService(source)
    source_service.create_packet(
        user_note="Мысль, которая должна переехать.",
        files=[("context.txt", "text/plain", b"portable context\n")],
        urls=["https://example.com/portable"],
    )
    (source.root / "personal-binary.bin").write_bytes(b"complete workspace payload")
    archive = create_workspace_archive(source.root, tmp_path / "portable.memorex.zip")

    with ZipFile(archive) as bundle:
        names = set(bundle.namelist())
    assert "manifest.json" in names
    assert "workspace/memorex.toml" in names
    assert "workspace/personal-binary.bin" in names
    assert "workspace/.memorex/wiki-first/state.sqlite" in names
    assert any(name.startswith("workspace/vault/") for name in names)

    target = WorkspaceSettings.create(tmp_path / "target", "Local before restore")
    target_service = WikiFirstService(target)
    target_service.create_packet(user_note="Локальная запись до восстановления.", files=[], urls=[])
    (target.root / "local-only.txt").write_text("keep me in safety backup", encoding="utf-8")

    restored = restore_workspace_archive(archive, target.root)

    assert restored.safety_backup is not None
    assert restored.safety_backup.is_file()
    restored_settings = WorkspaceSettings.load(target.root)
    assert restored_settings.name == "Portable memory"
    assert (target.root / "personal-binary.bin").read_bytes() == b"complete workspace payload"
    assert not (target.root / "local-only.txt").exists()
    packets = WikiStorage(restored_settings).packets()
    assert [packet["user_note"] for packet in packets] == ["Мысль, которая должна переехать."]

    recovered_local = restore_workspace_archive(
        restored.safety_backup, tmp_path / "recovered-local"
    )
    recovered_packets = WikiStorage(WorkspaceSettings.load(recovered_local.root)).packets()
    assert [packet["user_note"] for packet in recovered_packets] == [
        "Локальная запись до восстановления."
    ]
    assert (recovered_local.root / "local-only.txt").is_file()


def test_restore_rejects_paths_escaping_the_workspace(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.memorex.zip"
    manifest = {
        "format": "memorex-workspace",
        "version": 1,
        "created_at": "2026-08-24T00:00:00+00:00",
        "workspace_name": "Unsafe",
        "files": 1,
    }
    with ZipFile(archive, "w", ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest))
        bundle.writestr("workspace/../../escaped.txt", "must not escape")

    with pytest.raises(WorkspaceArchiveError, match="Unsafe backup path"):
        restore_workspace_archive(archive, tmp_path / "target")

    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path / "target").exists()
