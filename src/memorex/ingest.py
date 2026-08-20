from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from memorex.domain import SegmentDraft

PARSER_VERSION = "1"
MAX_SEGMENT_CHARS = 2_000
SUPPORTED_SUFFIXES = {".txt": "text/plain", ".md": "text/markdown", ".markdown": "text/markdown"}


class SourceValidationError(ValueError):
    """Raised when a source cannot be safely ingested."""


@dataclass(frozen=True)
class ParsedSource:
    path: Path
    data: bytes
    sha256: str
    mime_type: str
    normalized_text: str
    segments: list[SegmentDraft]


def parse_source(path: Path) -> ParsedSource:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise SourceValidationError(f"Source is not a regular file: {resolved}")
    mime_type = SUPPORTED_SUFFIXES.get(resolved.suffix.lower())
    if mime_type is None:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise SourceValidationError(
            f"Unsupported source type {resolved.suffix!r}; expected {supported}"
        )

    data = resolved.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceValidationError(f"Source is not valid UTF-8: {resolved}") from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    segments = segment_text(normalized)
    if not segments:
        raise SourceValidationError("Source contains no non-whitespace text")
    return ParsedSource(
        path=resolved,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        mime_type=mime_type,
        normalized_text=normalized,
        segments=segments,
    )


def segment_text(text: str, max_chars: int = MAX_SEGMENT_CHARS) -> list[SegmentDraft]:
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    headings = [
        (match.start(), match.group(1).strip())
        for match in re.finditer(r"(?m)^#{1,6}[ \t]+(.+?)[ \t]*$", text)
    ]
    segments: list[SegmentDraft] = []
    cursor = 0
    ordinal = 0
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text):
            break

        hard_end = min(cursor + max_chars, len(text))
        end = hard_end
        if hard_end < len(text):
            window = text[cursor:hard_end]
            candidates = (window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
            split_at = next((position for position in candidates if position >= max_chars // 2), -1)
            if split_at > 0:
                end = cursor + split_at
        while end > cursor and text[end - 1].isspace():
            end -= 1
        if end <= cursor:
            end = hard_end

        section = None
        for heading_offset, heading in headings:
            if heading_offset > cursor:
                break
            section = heading
        segments.append(
            SegmentDraft(
                ordinal=ordinal,
                text=text[cursor:end],
                section=section,
                char_start=cursor,
                char_end=end,
            )
        )
        ordinal += 1
        cursor = end
    return segments
