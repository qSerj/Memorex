from __future__ import annotations

import re
from pathlib import Path

from memorex.wiki_first.models import ValidationResult

WIKI_LINK = re.compile(r"\[\[([a-z0-9][a-z0-9-]*)\]\]")
CITATION = re.compile(r"\[([A-Z]\d+)\]")
SOURCE_LINK = re.compile(r"\[[^]]+\]\((\.\./sources/[^)]+)\)")
LINE_LOCATOR = re.compile(r"строк(?:а|и)?\s+(\d+)(?:[–-](\d+))?", re.IGNORECASE)
REFERENCE_SOURCE = re.compile(r"^\s*(?:-\s*)?\[([A-Z]\d+)\]:\s+(\.\./sources/\S+)")
HASH_LINE_LOCATOR = re.compile(r"#L(\d+)(?:-L?(\d+))?")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.md")


def validate_wiki(
    wiki_dir: Path, sources_dir: Path, base_wiki: Path | None = None
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    files = sorted(path for path in wiki_dir.rglob("*") if path.is_file())
    markdown = [path for path in files if path.suffix == ".md"]
    for path in files:
        if path.suffix != ".md":
            errors.append(f"Wiki contains a non-Markdown file: {path.relative_to(wiki_dir)}")
    readme = wiki_dir / "README.md"
    if not readme.is_file():
        errors.append("wiki/README.md is required")
    slugs = {path.stem for path in markdown if path.name != "README.md"}
    thematic = [path for path in markdown if path.name != "README.md"]
    for path in thematic:
        if path.parent != wiki_dir or not SLUG.fullmatch(path.name):
            errors.append(f"Page filename must be top-level ASCII kebab-case: {path.name}")
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if not lines or not lines[0].startswith("# "):
            errors.append(f"{path.name}: first line must be an H1 title")
        if "## Источники" not in text:
            errors.append(f"{path.name}: must end with an Источники section")
            body, source_block = text, ""
        else:
            body, source_block = text.rsplit("## Источники", 1)
            if source_block.strip() and not text.rstrip().endswith(source_block.rstrip()):
                errors.append(f"{path.name}: Источники must be the final section")
        for target in WIKI_LINK.findall(text):
            if target not in slugs:
                errors.append(f"{path.name}: unresolved Wiki link [[{target}]]")
        used = set(CITATION.findall(body))
        defined = set(CITATION.findall(source_block))
        if missing := sorted(used - defined):
            errors.append(f"{path.name}: undefined citations: {', '.join(missing)}")
        if unused := sorted(defined - used):
            warnings.append(f"{path.name}: unused source definitions: {', '.join(unused)}")
        located_definitions: set[str] = set()
        for source_line in source_block.splitlines():
            source_match = SOURCE_LINK.search(source_line)
            reference_match = REFERENCE_SOURCE.search(source_line)
            if source_match is not None:
                relative = source_match.group(1)
                located_definitions.update(CITATION.findall(source_line))
            elif reference_match is not None:
                located_definitions.add(reference_match.group(1))
                relative = reference_match.group(2)
            else:
                continue
            clean_relative = relative.split("#", 1)[0]
            target = (wiki_dir / clean_relative).resolve()
            try:
                target.relative_to(sources_dir.resolve())
            except ValueError:
                errors.append(f"{path.name}: source link escapes sources/: {relative}")
                continue
            if not target.is_file():
                errors.append(f"{path.name}: missing source file: {relative}")
                continue
            source_lines = len(target.read_text(encoding="utf-8").splitlines())
            locators = LINE_LOCATOR.findall(source_line) + HASH_LINE_LOCATOR.findall(relative)
            for start, end in locators:
                last = int(end or start)
                if last > source_lines:
                    errors.append(
                        f"{path.name}: line locator {start}-{last} exceeds {relative} "
                        f"({source_lines} lines)"
                    )
        if unlocated := sorted(defined - located_definitions):
            errors.append(
                f"{path.name}: source definitions without ../sources/ links: {', '.join(unlocated)}"
            )
        if len(thematic) > 1 and not WIKI_LINK.search(text):
            warnings.append(f"{path.name}: no links to related Wiki pages")
    total = sum(path.stat().st_size for path in markdown)
    if base_wiki is not None and base_wiki.exists():
        base_files = [path for path in base_wiki.rglob("*.md") if path.is_file()]
        base_total = sum(path.stat().st_size for path in base_files)
        if base_total and total > base_total * 1.5:
            warnings.append(
                f"Wiki grew by more than 50% ({base_total} -> {total} bytes); review for bloat"
            )
        if base_files and len(markdown) > len(base_files) * 1.5:
            warnings.append(
                f"Page count grew by more than 50% ({len(base_files)} -> {len(markdown)})"
            )
    return ValidationResult(
        errors=errors, warnings=warnings, pages=len(markdown), bytes_total=total
    )
