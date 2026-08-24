from __future__ import annotations

import json
import shutil
import sqlite3
import stat
import tempfile
import uuid
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from memorex.config import ConfigurationError, WorkspaceSettings
from memorex.wiki_first.storage import WikiStorage

ARCHIVE_FORMAT = "memorex-workspace"
ARCHIVE_VERSION = 1
MANIFEST_NAME = "manifest.json"
WORKSPACE_PREFIX = "workspace"
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
SQLITE_TRANSIENT_SUFFIXES = {"-journal", "-shm", "-wal"}


class WorkspaceArchiveError(ValueError):
    pass


@dataclass(frozen=True)
class RestoreResult:
    root: Path
    safety_backup: Path | None


def create_workspace_archive(root: Path, destination: Path) -> Path:
    root = root.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not (root / "memorex.toml").is_file():
        raise WorkspaceArchiveError(f"Not a Memorex workspace: {root}")
    if destination == root or destination.is_relative_to(root):
        raise WorkspaceArchiveError("A backup archive must be created outside its workspace")
    if destination.exists():
        raise WorkspaceArchiveError(f"Backup already exists: {destination}")

    entries = _workspace_entries(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": ARCHIVE_FORMAT,
        "version": ARCHIVE_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "workspace_name": WorkspaceSettings.load(root).name,
        "files": sum(path.is_file() for path in entries),
    }
    try:
        with tempfile.TemporaryDirectory(prefix="memorex-sqlite-") as temporary:
            sqlite_dir = Path(temporary)
            with ZipFile(
                destination,
                mode="x",
                compression=ZIP_DEFLATED,
                compresslevel=6,
                allowZip64=True,
            ) as archive:
                archive.writestr(
                    MANIFEST_NAME,
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                )
                for path in entries:
                    relative = path.relative_to(root)
                    archive_name = str(PurePosixPath(WORKSPACE_PREFIX, *relative.parts))
                    if path.is_dir():
                        archive.writestr(_directory_info(f"{archive_name}/", path), b"")
                    elif _is_sqlite(path):
                        snapshot = sqlite_dir / f"{uuid.uuid4().hex}{path.suffix}"
                        _backup_sqlite(path, snapshot)
                        archive.write(snapshot, archive_name)
                    else:
                        archive.write(path, archive_name)
        with ZipFile(destination) as archive:
            broken = archive.testzip()
        if broken is not None:
            raise WorkspaceArchiveError(f"Backup verification failed at {broken}")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def restore_workspace_archive(archive_path: Path, target: Path) -> RestoreResult:
    archive_path = archive_path.expanduser().resolve()
    target = target.expanduser().resolve()
    _validate_restore_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-restore-", dir=str(target.parent)))
    restored = staging / WORKSPACE_PREFIX
    safety_backup: Path | None = None
    old_target: Path | None = None
    installed = False
    try:
        _extract_archive(archive_path, staging)
        _validate_workspace(restored)
        if target.exists() and any(target.iterdir()):
            if not (target / "memorex.toml").is_file():
                raise WorkspaceArchiveError(
                    f"Refusing to overwrite a non-Memorex directory: {target}"
                )
            safety_backup = _safety_backup_path(target)
            create_workspace_archive(target, safety_backup)

        if target.exists():
            old_target = target.parent / f".{target.name}-before-restore-{uuid.uuid4().hex[:8]}"
            target.rename(old_target)
        restored.rename(target)
        installed = True
        _validate_workspace(target)
        if old_target is not None:
            _remove_tree(old_target)
        _remove_tree(staging)
        return RestoreResult(root=target, safety_backup=safety_backup)
    except Exception:
        if installed and target.exists():
            _remove_tree(target)
        if old_target is not None and old_target.exists() and not target.exists():
            old_target.rename(target)
        _remove_tree(staging)
        raise


def _workspace_entries(root: Path) -> list[Path]:
    entries: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise WorkspaceArchiveError(f"Workspace symlinks are not supported: {path}")
        if path.is_file() and any(
            path.name.endswith(suffix) for suffix in SQLITE_TRANSIENT_SUFFIXES
        ):
            continue
        entries.append(path)
    return entries


def _is_sqlite(path: Path) -> bool:
    if path.suffix.lower() not in SQLITE_SUFFIXES or path.stat().st_size < 16:
        return False
    with path.open("rb") as stream:
        return stream.read(16) == b"SQLite format 3\x00"


def _backup_sqlite(source: Path, destination: Path) -> None:
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    with (
        closing(sqlite3.connect(source_uri, uri=True)) as source_connection,
        closing(sqlite3.connect(destination)) as destination_connection,
    ):
        source_connection.backup(destination_connection)
        result = destination_connection.execute("PRAGMA integrity_check").fetchone()
    if result is None or result[0] != "ok":
        raise WorkspaceArchiveError(f"SQLite integrity check failed: {source}")


def _directory_info(name: str, source: Path) -> ZipInfo:
    info = ZipInfo(name)
    info.external_attr = (stat.S_IFDIR | (source.stat().st_mode & 0o777)) << 16
    return info


def _extract_archive(archive_path: Path, staging: Path) -> None:
    try:
        archive = ZipFile(archive_path)
    except (BadZipFile, OSError) as exc:
        raise WorkspaceArchiveError("This is not a readable Memorex backup") from exc
    with archive:
        broken = archive.testzip()
        if broken is not None:
            raise WorkspaceArchiveError(f"Backup is damaged at {broken}")
        manifest = _read_manifest(archive)
        if manifest.get("format") != ARCHIVE_FORMAT or manifest.get("version") != ARCHIVE_VERSION:
            raise WorkspaceArchiveError("Unsupported Memorex backup format or version")
        seen: set[PurePosixPath] = set()
        directory_modes: list[tuple[Path, int]] = []
        for info in archive.infolist():
            if info.filename == MANIFEST_NAME:
                continue
            relative = _safe_archive_path(info)
            if relative in seen:
                raise WorkspaceArchiveError(f"Duplicate backup entry: {relative}")
            seen.add(relative)
            destination = staging.joinpath(*relative.parts)
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                directory_modes.append((destination, (info.external_attr >> 16) & 0o777))
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("xb") as output:
                shutil.copyfileobj(source, output)
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                destination.chmod(mode)
        for directory, mode in sorted(directory_modes, reverse=True):
            if mode:
                directory.chmod(mode)


def _read_manifest(archive: ZipFile) -> dict[str, Any]:
    try:
        raw = archive.read(MANIFEST_NAME)
        manifest = json.loads(raw)
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceArchiveError("Memorex backup manifest is missing or invalid") from exc
    if not isinstance(manifest, dict):
        raise WorkspaceArchiveError("Memorex backup manifest must be an object")
    return manifest


def _safe_archive_path(info: ZipInfo) -> PurePosixPath:
    name = info.filename
    if "\\" in name:
        raise WorkspaceArchiveError(f"Unsafe backup path: {name}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise WorkspaceArchiveError(f"Unsafe backup path: {name}")
    if path.parts[0] != WORKSPACE_PREFIX:
        raise WorkspaceArchiveError(f"Unexpected backup entry: {name}")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise WorkspaceArchiveError(f"Backup symlinks are not supported: {name}")
    return path


def _validate_restore_target(target: Path) -> None:
    if target == Path(target.anchor) or target == Path.home().resolve():
        raise WorkspaceArchiveError(f"Refusing to replace a broad system directory: {target}")
    if target.exists() and not target.is_dir():
        raise WorkspaceArchiveError(f"Restore target is not a directory: {target}")


def _validate_workspace(root: Path) -> None:
    try:
        settings = WorkspaceSettings.load(root)
    except ConfigurationError as exc:
        raise WorkspaceArchiveError("Backup does not contain a Memorex workspace") from exc
    for database in sorted(root.rglob("*")):
        if not database.is_file() or not _is_sqlite(database):
            continue
        uri = f"{database.resolve().as_uri()}?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as exc:
            raise WorkspaceArchiveError(
                f"Cannot open restored SQLite database: {database}"
            ) from exc
        if result is None or result[0] != "ok":
            raise WorkspaceArchiveError(f"Restored SQLite database is damaged: {database}")
    wiki_storage = WikiStorage(settings)
    if wiki_storage.database_path.is_file():
        try:
            wiki_storage.verify_active()
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise WorkspaceArchiveError("Restored active Wiki failed integrity validation") from exc


def _safety_backup_path(target: Path) -> Path:
    backup_dir = target.parent / ".memorex-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return backup_dir / f"{target.name}-before-restore-{timestamp}.memorex.zip"


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for item in sorted(path.rglob("*"), reverse=True):
        with suppress(OSError):
            item.chmod(item.stat().st_mode | stat.S_IWUSR | (stat.S_IXUSR if item.is_dir() else 0))
    with suppress(OSError):
        path.chmod(path.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
    shutil.rmtree(path)
