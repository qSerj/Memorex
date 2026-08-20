from __future__ import annotations

from pathlib import Path

import pytest

from memorex.config import WorkspaceConfig
from memorex.ingest import SourceValidationError, parse_source, segment_text
from memorex.storage import Storage


def make_storage(tmp_path: Path) -> Storage:
    storage = Storage(WorkspaceConfig(tmp_path / ".memorex"))
    storage.initialize()
    return storage


def test_parse_source_normalizes_utf8_bom_and_newlines(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_bytes(b"\xef\xbb\xbf# Title\r\n\rFirst claim.\r\n")

    parsed = parse_source(source)

    assert parsed.normalized_text == "# Title\n\nFirst claim.\n"
    assert parsed.mime_type == "text/markdown"
    assert parsed.segments[0].section == "Title"
    for segment in parsed.segments:
        assert parsed.normalized_text[segment.char_start : segment.char_end] == segment.text


def test_segment_text_preserves_exact_non_overlapping_spans() -> None:
    text = "# Alpha\n\nFirst paragraph has words.\n\nSecond paragraph is also present."

    segments = segment_text(text, max_chars=30)

    assert len(segments) >= 2
    assert all(segment.char_end - segment.char_start <= 30 for segment in segments)
    assert all(text[segment.char_start : segment.char_end] == segment.text for segment in segments)
    assert all(
        left.char_end <= right.char_start
        for left, right in zip(segments, segments[1:], strict=False)
    )
    assert all(segment.section == "Alpha" for segment in segments)


def test_reingest_is_idempotent_and_changed_file_creates_revision(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    source = tmp_path / "source.md"
    source.write_text("Memorex uses SQLite.", encoding="utf-8")

    first = storage.ingest_source(parse_source(source))
    second = storage.ingest_source(parse_source(source))
    source.write_text("Memorex uses SQLite and FTS5.", encoding="utf-8")
    third = storage.ingest_source(parse_source(source))

    assert first["status"] == "added"
    assert second["status"] == "unchanged"
    assert second["version_id"] == first["version_id"]
    assert third["status"] == "revised"
    assert third["revision"] == 2
    details = storage.get_source(first["source_id"])
    assert [version["revision"] for version in details["versions"]] == [2, 1]
    assert len(list((tmp_path / ".memorex" / "objects").glob("*/*"))) == 2


def test_same_content_different_paths_reuses_object_but_not_source(tmp_path: Path) -> None:
    storage = make_storage(tmp_path)
    first_path = tmp_path / "first.txt"
    second_path = tmp_path / "second.txt"
    first_path.write_text("Shared content.", encoding="utf-8")
    second_path.write_text("Shared content.", encoding="utf-8")

    first = storage.ingest_source(parse_source(first_path))
    second = storage.ingest_source(parse_source(second_path))

    assert first["source_id"] != second["source_id"]
    assert first["sha256"] == second["sha256"]
    assert first["object_path"] == second["object_path"]
    assert len(list((tmp_path / ".memorex" / "objects").glob("*/*"))) == 1
    assert first_path.read_text(encoding="utf-8") == "Shared content."


@pytest.mark.parametrize("name", ["source.pdf", "source.json", "source"])
def test_unsupported_source_type_is_rejected(tmp_path: Path, name: str) -> None:
    source = tmp_path / name
    source.write_text("content", encoding="utf-8")

    with pytest.raises(SourceValidationError, match="Unsupported source type"):
        parse_source(source)


def test_invalid_utf8_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"\xff\xfe")

    with pytest.raises(SourceValidationError, match="not valid UTF-8"):
        parse_source(source)
