from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from memorex.config import WorkspaceSettings
from memorex.wiki_first.models import PacketUpload
from memorex.wiki_first.service import WikiFirstService
from memorex.wiki_first.storage import WikiStorage
from memorex.workspace_archive import (
    WorkspaceArchiveError,
    create_readable_export,
    create_workspace_archive,
    restore_workspace_archive,
)

PNG = b"\x89PNG\r\n\x1a\nportable-image"


def test_full_workspace_restore_preserves_previous_workspace_as_backup(tmp_path: Path) -> None:
    source = WorkspaceSettings.create(tmp_path / "source", "Portable memory")
    source_service = WikiFirstService(source)
    source_service.create_packet(
        user_note="Мысль, которая должна переехать.",
        files=[
            ("context.txt", "text/plain", b"portable context\n"),
            PacketUpload("scan.png", "image/png", PNG, "store"),
        ],
        urls=["https://example.com/portable"],
    )
    inbox = next(
        item for item in source_service.storage.notebooks() if item["system_key"] == "inbox"
    )
    note = source_service.create_note("Portable note", "Local-first body.", str(inbox["id"]))
    source_service.storage.add_note_attachment(
        str(note["id"]), name="document.pdf", mime_type="application/pdf", data=b"portable pdf"
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
    restored_storage = WikiStorage(restored_settings)
    restored_storage.initialize()
    packets = restored_storage.packets()
    assert [packet["user_note"] for packet in packets] == ["Мысль, которая должна переехать."]
    image_item = next(item for item in packets[0]["items"] if item["display_name"] == "scan.png")
    restored_image = restored_storage.packet_item(str(packets[0]["id"]), str(image_item["id"]))
    assert (restored_storage.root / str(restored_image["object_path"])).read_bytes() == PNG
    restored_note = restored_storage.note(str(note["id"]))
    restored_attachment = restored_note["attachments"][0]
    assert restored_attachment["display_name"] == "document.pdf"
    assert (
        restored_storage.root / restored_attachment["object_path"]
    ).read_bytes() == b"portable pdf"

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


def test_readable_export_contains_human_files_without_runtime_database(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "source", "Readable memory")
    service = WikiFirstService(settings)
    service.initialize()
    inbox = next(item for item in service.storage.notebooks() if item["system_key"] == "inbox")
    note = service.create_note("Readable note", "Body for a human.", str(inbox["id"]))
    attachment = service.storage.add_note_attachment(
        str(note["id"]), name="document.pdf", mime_type="application/pdf", data=b"pdf bytes"
    )
    export = create_readable_export(settings.root, tmp_path / "readable.zip")

    with ZipFile(export) as bundle:
        names = set(bundle.namelist())
        index = bundle.read("README.md").decode()
        note_text = bundle.read(f"notes/{note['slug']}.md").decode()
        attachment_name = f"attachments/{note['slug']}/{attachment['id']}-document.pdf"
        assert "Readable memory" in index
        assert "Readable note" in index
        assert "Body for a human." in note_text
        assert "## Приложенные файлы" in note_text
        assert bundle.read(attachment_name) == b"pdf bytes"
        assert not any(name.endswith((".sqlite", ".db")) for name in names)
        assert "manifest.json" not in names
        assert not any("jobs" in name for name in names)
