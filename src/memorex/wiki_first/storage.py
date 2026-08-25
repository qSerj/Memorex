from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from memorex.config import WorkspaceSettings
from memorex.wiki_first.models import PacketUpload

MIGRATIONS = Path(__file__).parent.parent / "migrations"
TEXT_SUFFIXES = {".md", ".txt"}
IMAGE_MIME_TYPES = {
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    canonical_path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_revisions (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    revision_no INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    object_path TEXT NOT NULL,
    normalized_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    applied_job_id TEXT,
    UNIQUE(source_id, revision_no),
    UNIQUE(source_id, sha256)
);
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES snapshots(id),
    relative_path TEXT NOT NULL,
    tree_hash TEXT NOT NULL,
    source_job_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activations (
    id INTEGER PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
    previous_snapshot_id TEXT REFERENCES snapshots(id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    base_snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
    runner TEXT NOT NULL,
    current_revision INTEGER NOT NULL DEFAULT 0,
    user_input TEXT,
    rejection_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_sources (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    source_revision_id INTEGER NOT NULL REFERENCES source_revisions(id),
    PRIMARY KEY(job_id, source_revision_id)
);
CREATE TABLE IF NOT EXISTS proposal_revisions (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    revision_no INTEGER NOT NULL,
    relative_path TEXT NOT NULL,
    tree_hash TEXT NOT NULL,
    feedback TEXT,
    validation_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(job_id, revision_no)
);
CREATE TABLE IF NOT EXISTS runner_calls (
    id INTEGER PRIMARY KEY,
    job_id TEXT,
    purpose TEXT NOT NULL,
    runner TEXT NOT NULL,
    model TEXT NOT NULL,
    command_version TEXT,
    prompt_version TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    stdout TEXT NOT NULL,
    stderr TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_input_tokens INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS answers (
    id INTEGER PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    runner TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES chat_sessions(id),
    role TEXT NOT NULL, content TEXT NOT NULL, snapshot_id TEXT REFERENCES snapshots(id),
    runner TEXT, model TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notebooks (
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, system_key TEXT UNIQUE,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE,
    notebook_id TEXT NOT NULL REFERENCES notebooks(id),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS note_attachments (
    id TEXT PRIMARY KEY, note_id TEXT NOT NULL REFERENCES notes(id),
    source_revision_id INTEGER NOT NULL REFERENCES source_revisions(id),
    display_name TEXT NOT NULL, mime_type TEXT NOT NULL,
    ordinal INTEGER NOT NULL, created_at TEXT NOT NULL, removed_at TEXT,
    UNIQUE(note_id, ordinal)
);
CREATE TABLE IF NOT EXISTS discussion_notes (
    session_id TEXT NOT NULL REFERENCES chat_sessions(id),
    note_id TEXT NOT NULL REFERENCES notes(id), ordinal INTEGER NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY(session_id, note_id)
);
CREATE TABLE IF NOT EXISTS discussion_turns (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES chat_sessions(id),
    question_message_id INTEGER NOT NULL REFERENCES chat_messages(id),
    answer_message_id INTEGER REFERENCES chat_messages(id), task_id TEXT,
    status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discussion_turn_notes (
    turn_id TEXT NOT NULL REFERENCES discussion_turns(id),
    note_id TEXT NOT NULL REFERENCES notes(id),
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id), ordinal INTEGER NOT NULL,
    PRIMARY KEY(turn_id, note_id)
);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, job_id TEXT, phase TEXT NOT NULL,
    payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
    slug UNINDEXED, title, text, links, backlinks, tokenize='unicode61'
);
"""

WIKI_LINK = re.compile(r"\[\[([a-z0-9][a-z0-9-]*)\]\]")
WORD = re.compile(r"[^\W_]{3,}", re.UNICODE)
PACKET_RETRY_DELAYS_SECONDS = (5, 30)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class WikiStorage:
    def __init__(self, settings: WorkspaceSettings):
        self.settings = settings
        self.root = settings.data.data_dir / "wiki-first"
        self.database_path = self.root / "state.sqlite"
        self.objects_dir = self.root / "objects"
        self.snapshots_dir = self.root / "snapshots"
        self.jobs_dir = self.root / "jobs"
        self.answers_dir = self.root / "answers"

    def initialize(self) -> dict[str, Any]:
        for path in (
            self.objects_dir,
            self.snapshots_dir,
            self.jobs_dir,
            self.answers_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript(SCHEMA)
            self._apply_migrations(connection)
            self._ensure_runner_call_columns(connection)
            self._ensure_proposal_columns(connection)
            active = connection.execute(
                "SELECT snapshot_id FROM activations ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if active is None:
                snapshot_id = "initial"
                snapshot = self.snapshots_dir / snapshot_id
                (snapshot / "wiki").mkdir(parents=True, exist_ok=True)
                (snapshot / "sources").mkdir(exist_ok=True)
                readme = snapshot / "wiki" / "README.md"
                readme.write_text("# Wiki\n\nБаза знаний пока пуста.\n", encoding="utf-8")
                tree_hash = hash_tree(snapshot)
                make_tree_read_only(snapshot)
                now = utc_now()
                connection.execute(
                    "INSERT INTO snapshots VALUES (?, NULL, ?, ?, NULL, ?)",
                    (snapshot_id, str(snapshot.relative_to(self.root)), tree_hash, now),
                )
                connection.execute(
                    "INSERT INTO activations("
                    "snapshot_id, previous_snapshot_id, reason, created_at) "
                    "VALUES (?, NULL, 'initialize', ?)",
                    (snapshot_id, now),
                )
        self.reconcile_packet_queue()
        self.reconcile_notes()
        self.rebuild_fts()
        self.sync_vault()
        return {"root": str(self.root), "snapshot": self.active_snapshot()["id"]}

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def active_snapshot(self) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT s.* FROM activations a JOIN snapshots s ON s.id = a.snapshot_id "
                "ORDER BY a.id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise ValueError("Wiki-first storage is not initialized")
        return dict(row)

    def snapshot_path(self, snapshot: dict[str, Any]) -> Path:
        return self.root / str(snapshot["relative_path"])

    def register_source(self, path: Path, *, kind: str = "file") -> dict[str, Any]:
        return self.register_text_bytes(
            path.read_bytes(), canonical_path=str(path.resolve()), kind=kind
        )

    def register_text_bytes(
        self, data: bytes, *, canonical_path: str, kind: str = "file"
    ) -> dict[str, Any]:
        checksum, object_path, normalized_path = self._prepare_text_bytes(data, canonical_path)
        with self.connection() as connection:
            return self._register_text(
                connection,
                canonical_path=canonical_path,
                kind=kind,
                checksum=checksum,
                object_path=object_path,
                normalized_path=normalized_path,
            )

    def _register_text(
        self,
        connection: sqlite3.Connection,
        *,
        canonical_path: str,
        kind: str,
        checksum: str,
        object_path: Path,
        normalized_path: Path,
    ) -> dict[str, Any]:
        now = utc_now()
        source = connection.execute(
            "SELECT * FROM sources WHERE canonical_path = ?", (canonical_path,)
        ).fetchone()
        if source is None:
            cursor = connection.execute(
                "INSERT INTO sources(canonical_path, kind, created_at) VALUES (?, ?, ?)",
                (canonical_path, kind, now),
            )
            source_id = int(cursor.lastrowid)
            revision_no = 1
        else:
            source_id = int(source["id"])
            current = connection.execute(
                "SELECT * FROM source_revisions WHERE source_id = ? "
                "ORDER BY revision_no DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            if current is not None and current["sha256"] == checksum:
                return {
                    **dict(current),
                    "status": "unchanged",
                    "canonical_path": canonical_path,
                }
            revision_no = int(current["revision_no"]) + 1 if current else 1
        cursor = connection.execute(
            "INSERT INTO source_revisions(source_id, revision_no, sha256, object_path, "
            "normalized_path, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                source_id,
                revision_no,
                checksum,
                str(object_path.relative_to(self.root)),
                str(normalized_path.relative_to(self.root)),
                now,
            ),
        )
        revision_id = int(cursor.lastrowid)
        return {
            "id": revision_id,
            "source_id": source_id,
            "revision_no": revision_no,
            "sha256": checksum,
            "normalized_path": str(normalized_path.relative_to(self.root)),
            "status": "added" if revision_no == 1 else "changed",
            "canonical_path": canonical_path,
        }

    def register_user_text(self, text: str, label: str) -> dict[str, Any]:
        if not text.strip():
            raise ValueError("User note must not be empty")
        note_dir = self.root / "user-input"
        note_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        note = note_dir / f"{stamp}-{label}.md"
        note.write_text(f"# Сообщение пользователя\n\n{text.strip()}\n", encoding="utf-8")
        return self.register_source(note, kind="user")

    def create_packet(
        self,
        *,
        user_note: str,
        files: list[PacketUpload],
        urls: list[str],
    ) -> dict[str, Any]:
        note = user_note.strip()
        if not note and not files and not urls:
            raise ValueError("Packet must contain a note, file, or URL")
        validated_files: list[tuple[PacketUpload, str, str]] = []
        for upload in files:
            name = upload.name
            suffix = Path(name).suffix.lower()
            if (
                name != Path(name).name
                or "\\" in name
                or suffix not in TEXT_SUFFIXES | IMAGE_MIME_TYPES.keys()
            ):
                raise ValueError(f"Unsafe or unsupported Packet filename: {name}")
            instruction = upload.analysis_instruction.strip()
            if len(instruction) > 2000:
                raise ValueError(f"Image analysis instruction is too long: {name}")
            if suffix in TEXT_SUFFIXES:
                if upload.processing_mode != "analyze":
                    raise ValueError(f"Text Packet files must be analyzed: {name}")
                try:
                    upload.data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError(f"Source is not UTF-8: {name}") from exc
                mime_type = "text/markdown" if suffix == ".md" else "text/plain"
                source_kind = "file"
            else:
                mime_type = _image_mime_type(upload.data)
                if mime_type != IMAGE_MIME_TYPES[suffix]:
                    raise ValueError(f"Image content does not match its filename: {name}")
                source_kind = "image"
            validated_files.append((upload, mime_type, source_kind))
        if any(not url.strip() for url in urls):
            raise ValueError("Packet URLs must not be empty")

        packet_id = uuid.uuid4().hex[:12]
        now = utc_now()
        prepared_note: tuple[str, Path, Path] | None = None
        if note:
            note_data = f"# Сообщение пользователя\n\n{note}\n".encode()
            prepared_note = self._prepare_text_bytes(note_data, f"packet://{packet_id}/note.md")
        prepared_files = []
        for ordinal, (upload, mime_type, source_kind) in enumerate(validated_files):
            item_id = uuid.uuid4().hex[:12]
            name = upload.name
            canonical = f"packet://{packet_id}/{item_id}/{name}"
            prepared = (
                self._prepare_text_bytes(upload.data, canonical)
                if source_kind == "file"
                else self._prepare_image_bytes(upload.data, canonical, mime_type)
            )
            prepared_files.append(
                (
                    item_id,
                    ordinal,
                    name,
                    mime_type,
                    canonical,
                    source_kind,
                    upload.processing_mode,
                    upload.analysis_instruction.strip(),
                    prepared,
                )
            )

        with self.connection() as connection:
            note_revision_id = None
            if prepared_note is not None:
                checksum, object_path, normalized_path = prepared_note
                revision = self._register_text(
                    connection,
                    canonical_path=f"packet://{packet_id}/note.md",
                    kind="user",
                    checksum=checksum,
                    object_path=object_path,
                    normalized_path=normalized_path,
                )
                note_revision_id = int(revision["id"])
            connection.execute(
                "INSERT INTO packets(id,user_note,note_source_revision_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?)",
                (packet_id, note, note_revision_id, now, now),
            )
            for (
                item_id,
                ordinal,
                name,
                mime_type,
                canonical,
                source_kind,
                processing_mode,
                instruction,
                prepared,
            ) in prepared_files:
                checksum, object_path, normalized_path = prepared
                revision = self._register_text(
                    connection,
                    canonical_path=canonical,
                    kind=source_kind,
                    checksum=checksum,
                    object_path=object_path,
                    normalized_path=normalized_path,
                )
                connection.execute(
                    "INSERT INTO packet_items(id,packet_id,ordinal,kind,display_name,mime_type,"
                    "source_revision_id,status,processing_mode,analysis_instruction,created_at) "
                    "VALUES (?,?,?,?,?,?,?,'ready',?,?,?)",
                    (
                        item_id,
                        packet_id,
                        ordinal,
                        "file",
                        name,
                        mime_type,
                        int(revision["id"]),
                        processing_mode,
                        instruction,
                        now,
                    ),
                )
            offset = len(prepared_files)
            for index, url in enumerate(urls):
                connection.execute(
                    "INSERT INTO packet_items(id,packet_id,ordinal,kind,display_name,url,status,"
                    "created_at) VALUES (?,?,?,?,?,?,'waiting_importer',?)",
                    (
                        uuid.uuid4().hex[:12],
                        packet_id,
                        offset + index,
                        "url",
                        url,
                        url,
                        now,
                    ),
                )
            if note_revision_id is not None or any(item[6] == "analyze" for item in prepared_files):
                connection.execute(
                    "INSERT INTO packet_queue(packet_id,status,attempt_count,available_at,"
                    "created_at,updated_at) VALUES (?,'queued',0,?,?,?)",
                    (packet_id, now, now, now),
                )
        return self.packet(packet_id)

    def packet(self, packet_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM packets WHERE id = ?", (packet_id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown Packet: {packet_id}")
            items = connection.execute(
                "SELECT * FROM packet_items WHERE packet_id = ? ORDER BY ordinal", (packet_id,)
            ).fetchall()
            job = connection.execute(
                "SELECT j.* FROM job_packets jp JOIN jobs j ON j.id = jp.job_id "
                "WHERE jp.packet_id = ? ORDER BY j.created_at DESC LIMIT 1",
                (packet_id,),
            ).fetchone()
            queue = connection.execute(
                "SELECT * FROM packet_queue WHERE packet_id = ?", (packet_id,)
            ).fetchone()
            attempts = connection.execute(
                "SELECT j.* FROM job_packets jp JOIN jobs j ON j.id = jp.job_id "
                "WHERE jp.packet_id = ? ORDER BY j.created_at DESC",
                (packet_id,),
            ).fetchall()
            latest_event = None
            if job is not None:
                event = connection.execute(
                    "SELECT * FROM task_events WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                    (job["id"],),
                ).fetchone()
                runner_event = connection.execute(
                    "SELECT * FROM task_events WHERE job_id = ? AND phase = 'runner' "
                    "ORDER BY id DESC LIMIT 1",
                    (job["id"],),
                ).fetchone()
                if event is not None:
                    latest_event = {
                        **dict(event),
                        "payload": json.loads(str(event["payload_json"])),
                    }
                    if runner_event is not None:
                        runner_payload = json.loads(str(runner_event["payload_json"]))
                        latest_event["payload"].setdefault("runner", runner_payload.get("runner"))
                        latest_event["payload"].setdefault("model", runner_payload.get("model"))
        result = dict(row)
        result["items"] = [dict(item) for item in items]
        result["latest_job"] = dict(job) if job else None
        result["latest_event"] = latest_event
        result["queue"] = dict(queue) if queue else None
        result["attempts"] = [dict(attempt) for attempt in attempts]
        result["processable_count"] = len(self.packet_source_revisions(packet_id))
        result["waiting_importer_count"] = sum(
            item["status"] == "waiting_importer" for item in result["items"]
        )
        result["stored_only_count"] = sum(
            item["kind"] == "file" and item["processing_mode"] == "store"
            for item in result["items"]
        )
        result["state"] = self._packet_state(result)
        return result

    def packets(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM packets ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self.packet(str(row["id"])) for row in rows]

    def packet_source_revisions(self, packet_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            packet = connection.execute(
                "SELECT note_source_revision_id FROM packets WHERE id = ?", (packet_id,)
            ).fetchone()
            if packet is None:
                raise ValueError(f"Unknown Packet: {packet_id}")
            items = connection.execute(
                "SELECT sr.*, s.canonical_path, s.kind, pi.mime_type, pi.processing_mode, "
                "pi.analysis_instruction, pi.ordinal FROM packet_items pi "
                "JOIN source_revisions sr ON sr.id = pi.source_revision_id "
                "JOIN sources s ON s.id = sr.source_id WHERE pi.packet_id = ? "
                "AND pi.source_revision_id IS NOT NULL AND pi.processing_mode = 'analyze' "
                "AND sr.applied_job_id IS NULL ORDER BY pi.ordinal",
                (packet_id,),
            ).fetchall()
            rows: list[dict[str, Any]] = []
            if packet["note_source_revision_id"] is not None:
                note = connection.execute(
                    "SELECT sr.*, s.canonical_path, s.kind FROM source_revisions sr "
                    "JOIN sources s ON s.id = sr.source_id WHERE sr.id = ? "
                    "AND sr.applied_job_id IS NULL",
                    (packet["note_source_revision_id"],),
                ).fetchone()
                if note is not None:
                    rows.append(
                        {
                            **dict(note),
                            "mime_type": "text/markdown",
                            "processing_mode": "analyze",
                            "analysis_instruction": "",
                            "ordinal": -1,
                        }
                    )
            rows.extend(dict(item) for item in items)
        return rows

    def packet_item(self, packet_id: str, item_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT pi.*, sr.object_path, sr.sha256, s.kind AS source_kind "
                "FROM packet_items pi LEFT JOIN source_revisions sr "
                "ON sr.id = pi.source_revision_id LEFT JOIN sources s ON s.id = sr.source_id "
                "WHERE pi.packet_id = ? AND pi.id = ?",
                (packet_id, item_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown Packet item: {item_id}")
        return dict(row)

    def _packet_state(self, packet: dict[str, Any]) -> str:
        queue = packet.get("queue")
        if queue:
            return {
                "queued": "queued",
                "running": "processing",
                "retry_wait": "retry_wait",
                "review": "review",
                "done": self._completed_packet_state(packet),
                "failed": "failed",
                "idle": "ready",
            }[str(queue["status"])]
        job = packet.get("latest_job")
        if job:
            status = str(job["status"])
            return {
                "running": "processing",
                "proposed": "review",
                "applied": "remembered",
                "no_change": "processed",
                "failed": "failed",
                "cancelled": "failed",
                "rejected": "ready",
            }.get(status, status)
        if packet["processable_count"]:
            return "ready"
        if packet["waiting_importer_count"]:
            return "waiting_importer"
        return "stored"

    @staticmethod
    def _completed_packet_state(packet: dict[str, Any]) -> str:
        job = packet.get("latest_job")
        return "remembered" if job and job["status"] == "applied" else "processed"

    def reconcile_packet_queue(self) -> int:
        """Create durable queue state for Packets saved by earlier application versions."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT p.id FROM packets p LEFT JOIN packet_queue q ON q.packet_id = p.id "
                "WHERE q.packet_id IS NULL ORDER BY p.created_at"
            ).fetchall()
        inserted = 0
        for row in rows:
            packet_id = str(row["id"])
            if not self.packet_source_revisions(packet_id):
                continue
            with self.connection() as connection:
                latest = connection.execute(
                    "SELECT j.* FROM job_packets jp JOIN jobs j ON j.id = jp.job_id "
                    "WHERE jp.packet_id = ? ORDER BY j.created_at DESC LIMIT 1",
                    (packet_id,),
                ).fetchone()
                attempts = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM job_packets WHERE packet_id = ?", (packet_id,)
                    ).fetchone()[0]
                )
                status = (
                    {
                        "running": "queued",
                        "proposed": "review",
                        "failed": "failed",
                        "cancelled": "failed",
                        "rejected": "idle",
                    }.get(str(latest["status"]), "queued")
                    if latest
                    else "queued"
                )
                last_error = (
                    str(latest["rejection_reason"])
                    if latest and latest["rejection_reason"]
                    else None
                )
                now = utc_now()
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO packet_queue(packet_id,status,attempt_count,"
                    "available_at,last_job_id,last_error,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        packet_id,
                        status,
                        attempts,
                        now,
                        str(latest["id"]) if latest else None,
                        last_error,
                        now,
                        now,
                    ),
                )
                inserted += int(cursor.rowcount)
        return inserted

    def packet_queue(self, packet_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM packet_queue WHERE packet_id = ?", (packet_id,)
            ).fetchone()
        return dict(row) if row else None

    def queue_packet(self, packet_id: str, *, reset_attempts: bool = False) -> dict[str, Any]:
        if not self.packet_source_revisions(packet_id):
            raise ValueError("Packet has no text sources awaiting processing")
        now = utc_now()
        with self.connection() as connection:
            queue = connection.execute(
                "SELECT * FROM packet_queue WHERE packet_id = ?", (packet_id,)
            ).fetchone()
            if queue is None:
                connection.execute(
                    "INSERT INTO packet_queue(packet_id,status,attempt_count,available_at,"
                    "created_at,updated_at) VALUES (?,'queued',0,?,?,?)",
                    (packet_id, now, now, now),
                )
            elif queue["status"] not in {"queued", "running", "retry_wait", "review"}:
                connection.execute(
                    "UPDATE packet_queue SET status = 'queued', attempt_count = ?, "
                    "available_at = ?, last_error = NULL, updated_at = ? WHERE packet_id = ?",
                    (0 if reset_attempts else int(queue["attempt_count"]), now, now, packet_id),
                )
        queued = self.packet_queue(packet_id)
        if queued is None:
            raise ValueError(f"Could not queue Packet: {packet_id}")
        return queued

    def begin_packet_attempt(self, packet_id: str) -> dict[str, Any]:
        self.queue_packet(packet_id)
        now = utc_now()
        with self.connection() as connection:
            queue = connection.execute(
                "SELECT * FROM packet_queue WHERE packet_id = ?", (packet_id,)
            ).fetchone()
            if queue is None:
                raise ValueError(f"Unknown Packet queue: {packet_id}")
            if queue["status"] == "review":
                raise ValueError("Packet already has a proposal awaiting review")
            if queue["status"] != "running":
                connection.execute(
                    "UPDATE packet_queue SET status = 'running', "
                    "attempt_count = attempt_count + 1, available_at = ?, "
                    "last_error = NULL, updated_at = ? WHERE packet_id = ?",
                    (now, now, packet_id),
                )
        started = self.packet_queue(packet_id)
        if started is None:
            raise ValueError(f"Unknown Packet queue: {packet_id}")
        return started

    def claim_next_packet(self, *, now: str | None = None) -> dict[str, Any] | None:
        available = now or utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM packet_queue WHERE "
                "(status = 'queued' OR (status = 'retry_wait' AND available_at <= ?)) "
                "AND NOT EXISTS (SELECT 1 FROM jobs WHERE status = 'running') "
                "ORDER BY available_at, created_at LIMIT 1",
                (available,),
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                "UPDATE packet_queue SET status = 'running', attempt_count = attempt_count + 1, "
                "last_error = NULL, updated_at = ? WHERE packet_id = ? "
                "AND status IN ('queued','retry_wait')",
                (available, row["packet_id"]),
            )
            if cursor.rowcount != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM packet_queue WHERE packet_id = ?", (row["packet_id"],)
            ).fetchone()
        return dict(claimed) if claimed else None

    def fail_packet_attempt(
        self,
        packet_id: str,
        *,
        job_id: str | None,
        error: str,
        retryable: bool,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        with self.connection() as connection:
            queue = connection.execute(
                "SELECT * FROM packet_queue WHERE packet_id = ?", (packet_id,)
            ).fetchone()
            if queue is None:
                raise ValueError(f"Unknown Packet queue: {packet_id}")
            attempt = int(queue["attempt_count"])
            can_retry = retryable and 0 < attempt <= len(PACKET_RETRY_DELAYS_SECONDS)
            delay = PACKET_RETRY_DELAYS_SECONDS[attempt - 1] if can_retry else 0
            available_at = (now + timedelta(seconds=delay)).isoformat()
            connection.execute(
                "UPDATE packet_queue SET status = ?, available_at = ?, last_job_id = ?, "
                "last_error = ?, updated_at = ? WHERE packet_id = ?",
                (
                    "retry_wait" if can_retry else "failed",
                    available_at,
                    job_id,
                    error,
                    now.isoformat(),
                    packet_id,
                ),
            )
        failed = self.packet_queue(packet_id)
        if failed is None:
            raise ValueError(f"Unknown Packet queue: {packet_id}")
        return failed

    def pending_revisions(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT sr.*, s.canonical_path, s.kind FROM source_revisions sr "
                "JOIN sources s ON s.id = sr.source_id "
                "JOIN (SELECT source_id, MAX(revision_no) revision_no FROM source_revisions "
                "GROUP BY source_id) current ON current.source_id = sr.source_id "
                "AND current.revision_no = sr.revision_no WHERE sr.applied_job_id IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM packets p "
                "WHERE p.note_source_revision_id = sr.id) "
                "AND NOT EXISTS (SELECT 1 FROM packet_items pi "
                "WHERE pi.source_revision_id = sr.id) "
                "ORDER BY sr.id"
            ).fetchall()
        return [dict(row) for row in rows]

    def source_revision(self, revision_id: int) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT sr.*, s.canonical_path, s.kind FROM source_revisions sr "
                "JOIN sources s ON s.id = sr.source_id WHERE sr.id = ?",
                (revision_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown source revision: {revision_id}")
        return dict(row)

    def create_job(
        self,
        job_id: str,
        *,
        kind: str,
        runner: str,
        source_revision_ids: list[int],
        user_input: str | None = None,
        packet_id: str | None = None,
    ) -> dict[str, Any]:
        base = self.active_snapshot()
        now = utc_now()
        with self.connection() as connection:
            unfinished = connection.execute(
                "SELECT id FROM jobs WHERE status = 'running' LIMIT 1"
            ).fetchone()
            if unfinished is not None:
                raise ValueError(f"Wait for running job {unfinished['id']} to finish")
            connection.execute(
                "INSERT INTO jobs(id, kind, status, base_snapshot_id, runner, user_input, "
                "created_at, updated_at) VALUES (?, ?, 'running', ?, ?, ?, ?, ?)",
                (job_id, kind, base["id"], runner, user_input, now, now),
            )
            connection.executemany(
                "INSERT INTO job_sources(job_id, source_revision_id) VALUES (?, ?)",
                [(job_id, revision_id) for revision_id in source_revision_ids],
            )
            if packet_id is not None:
                connection.execute(
                    "INSERT INTO job_packets(job_id,packet_id) VALUES (?,?)",
                    (job_id, packet_id),
                )
                connection.execute(
                    "UPDATE packet_queue SET last_job_id = ?, updated_at = ? WHERE packet_id = ?",
                    (job_id, now, packet_id),
                )
        return self.get_job(job_id)

    def recover_interrupted_jobs(self) -> int:
        """Recover abandoned work, preserving an earlier reviewable proposal."""
        with self.connection() as connection:
            now = utc_now()
            restorable = connection.execute(
                "SELECT id FROM jobs WHERE status = 'running' AND current_revision > 0 "
                "AND EXISTS (SELECT 1 FROM proposal_revisions pr WHERE pr.job_id = jobs.id "
                "AND pr.revision_no = jobs.current_revision)"
            ).fetchall()
            restorable_ids = [str(row["id"]) for row in restorable]
            if restorable_ids:
                placeholders = ",".join("?" for _ in restorable_ids)
                message = "revision was interrupted; previous proposal preserved"
                connection.execute(
                    f"UPDATE jobs SET status = 'proposed', rejection_reason = ?, "
                    f"updated_at = ? WHERE id IN ({placeholders})",
                    (message, now, *restorable_ids),
                )
                connection.execute(
                    f"UPDATE packet_queue SET status = 'review', last_error = ?, updated_at = ? "
                    f"WHERE last_job_id IN ({placeholders})",
                    (message, now, *restorable_ids),
                )
            failed = connection.execute(
                "UPDATE jobs SET status = 'failed', "
                "rejection_reason = 'server or CLI process was interrupted', updated_at = ? "
                "WHERE status = 'running'",
                (now,),
            )
            connection.execute(
                "UPDATE packet_queue SET status = 'queued', available_at = ?, "
                "last_error = 'analysis was interrupted; it will resume', updated_at = ? "
                "WHERE status = 'running'",
                (now, now),
            )
            return len(restorable_ids) + int(failed.rowcount)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT j.*, jp.packet_id FROM jobs j LEFT JOIN job_packets jp "
                "ON jp.job_id = j.id WHERE j.id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown Wiki proposal: {job_id}")
            sources = connection.execute(
                "SELECT source_revision_id FROM job_sources WHERE job_id = ? "
                "ORDER BY source_revision_id",
                (job_id,),
            ).fetchall()
        result = dict(row)
        result["source_revision_ids"] = [int(item[0]) for item in sources]
        return result

    def add_proposal_revision(
        self,
        job_id: str,
        *,
        revision_no: int,
        relative_path: str,
        tree_hash: str,
        validation_json: str,
        feedback: str | None,
        retrieval_json: str = "{}",
        base_snapshot_id: str | None = None,
    ) -> None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO proposal_revisions(job_id, revision_no, relative_path, tree_hash, "
                "feedback, validation_json, created_at, retrieval_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    revision_no,
                    relative_path,
                    tree_hash,
                    feedback,
                    validation_json,
                    now,
                    retrieval_json,
                ),
            )
            if base_snapshot_id is None:
                connection.execute(
                    "UPDATE jobs SET status = 'proposed', current_revision = ?, "
                    "rejection_reason = NULL, updated_at = ? WHERE id = ?",
                    (revision_no, now, job_id),
                )
            else:
                connection.execute(
                    "UPDATE jobs SET status = 'proposed', current_revision = ?, "
                    "base_snapshot_id = ?, rejection_reason = NULL, updated_at = ? WHERE id = ?",
                    (revision_no, base_snapshot_id, now, job_id),
                )
            connection.execute(
                "UPDATE packet_queue SET status = 'review', available_at = ?, "
                "last_job_id = ?, last_error = NULL, updated_at = ? "
                "WHERE packet_id = (SELECT packet_id FROM job_packets WHERE job_id = ?)",
                (now, job_id, now, job_id),
            )

    def restore_proposal_after_failed_revision(self, job_id: str, error: str) -> None:
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = 'proposed', rejection_reason = ?, updated_at = ? "
                "WHERE id = ? AND status = 'running' AND current_revision > 0",
                (error, now, job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Job {job_id} has no proposal to restore")
            connection.execute(
                "UPDATE packet_queue SET status = 'review', available_at = ?, "
                "last_job_id = ?, last_error = ?, updated_at = ? "
                "WHERE packet_id = (SELECT packet_id FROM job_packets WHERE job_id = ?)",
                (now, job_id, error, now, job_id),
            )

    def requeue_stale_proposal(self, job_id: str, reason: str) -> None:
        now = utc_now()
        with self.connection() as connection:
            packet = connection.execute(
                "SELECT packet_id FROM job_packets WHERE job_id = ?", (job_id,)
            ).fetchone()
            if packet is None:
                raise ValueError("Only Packet proposals can be requeued automatically")
            connection.execute(
                "UPDATE jobs SET status = 'stale', rejection_reason = ?, updated_at = ? "
                "WHERE id = ? AND status = 'proposed'",
                (reason, now, job_id),
            )
            connection.execute(
                "UPDATE packet_queue SET status = 'queued', available_at = ?, "
                "last_error = ?, updated_at = ? WHERE packet_id = ?",
                (now, reason, now, packet["packet_id"]),
            )

    def finish_job_without_changes(
        self,
        job_id: str,
        *,
        relative_path: str,
        tree_hash: str,
        validation_json: str,
        retrieval_json: str,
    ) -> None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO proposal_revisions(job_id,revision_no,relative_path,tree_hash,"
                "feedback,validation_json,created_at,retrieval_json) "
                "VALUES (?,1,?,?,NULL,?,?,?)",
                (
                    job_id,
                    relative_path,
                    tree_hash,
                    validation_json,
                    now,
                    retrieval_json,
                ),
            )
            connection.execute(
                "UPDATE jobs SET status = 'no_change', current_revision = 1, updated_at = ? "
                "WHERE id = ?",
                (now, job_id),
            )
            connection.execute(
                "UPDATE source_revisions SET applied_job_id = ? WHERE id IN "
                "(SELECT source_revision_id FROM job_sources WHERE job_id = ?)",
                (job_id, job_id),
            )
            connection.execute(
                "UPDATE packet_queue SET status = 'done', available_at = ?, "
                "last_job_id = ?, last_error = NULL, updated_at = ? "
                "WHERE packet_id = (SELECT packet_id FROM job_packets WHERE job_id = ?)",
                (now, job_id, now, job_id),
            )

    def proposal(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM proposal_revisions WHERE job_id = ? AND revision_no = ?",
                (job_id, job["current_revision"]),
            ).fetchone()
        if row is None:
            raise ValueError(f"Proposal {job_id} has no completed revision")
        return {**job, **dict(row)}

    def record_call(self, values: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO runner_calls(job_id, purpose, runner, model, command_version, "
                "prompt_version, duration_ms, status, stdout, stderr, input_tokens, "
                "output_tokens, cached_input_tokens, profile, effort, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    values.get("job_id"),
                    values["purpose"],
                    values["runner"],
                    values["model"],
                    values.get("command_version"),
                    values["prompt_version"],
                    values["duration_ms"],
                    values["status"],
                    values.get("stdout", ""),
                    values.get("stderr", ""),
                    values.get("input_tokens"),
                    values.get("output_tokens"),
                    values.get("cached_input_tokens"),
                    values.get("profile"),
                    values.get("effort"),
                    utc_now(),
                ),
            )

    def latest_successful_call(self, job_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM runner_calls WHERE job_id = ? AND status = 'succeeded' "
                "ORDER BY id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def activate(self, job_id: str, snapshot_id: str, relative_path: str, tree_hash: str) -> None:
        job = self.get_job(job_id)
        active = self.active_snapshot()
        if active["id"] != job["base_snapshot_id"]:
            raise ValueError("Proposal is stale because the active Wiki changed")
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?)",
                (snapshot_id, active["id"], relative_path, tree_hash, job_id, now),
            )
            connection.execute(
                "INSERT INTO activations(snapshot_id, previous_snapshot_id, reason, created_at) "
                "VALUES (?, ?, ?, ?)",
                (snapshot_id, active["id"], f"apply:{job_id}", now),
            )
            connection.execute(
                "UPDATE jobs SET status = 'applied', updated_at = ? WHERE id = ?",
                (now, job_id),
            )
            connection.execute(
                "UPDATE source_revisions SET applied_job_id = ? WHERE id IN "
                "(SELECT source_revision_id FROM job_sources WHERE job_id = ?)",
                (job_id, job_id),
            )
            connection.execute(
                "UPDATE packet_queue SET status = 'done', available_at = ?, "
                "last_job_id = ?, last_error = NULL, updated_at = ? "
                "WHERE packet_id = (SELECT packet_id FROM job_packets WHERE job_id = ?)",
                (now, job_id, now, job_id),
            )

    def reject(self, job_id: str, reason: str) -> None:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = 'rejected', rejection_reason = ?, updated_at = ? "
                "WHERE id = ? AND status = 'proposed'",
                (reason, utc_now(), job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Proposal {job_id} is not awaiting review")
            connection.execute(
                "UPDATE packet_queue SET status = 'idle', available_at = ?, "
                "last_job_id = ?, last_error = NULL, updated_at = ? "
                "WHERE packet_id = (SELECT packet_id FROM job_packets WHERE job_id = ?)",
                (utc_now(), job_id, utc_now(), job_id),
            )

    def fail_job(self, job_id: str, reason: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'failed', rejection_reason = ?, updated_at = ? "
                "WHERE id = ?",
                (reason, utc_now(), job_id),
            )

    def cancel_job(self, job_id: str, reason: str = "stopped by user") -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'cancelled', rejection_reason = ?, updated_at = ? "
                "WHERE id = ? AND status = 'running'",
                (reason, utc_now(), job_id),
            )

    def set_job_runner(self, job_id: str, runner: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE jobs SET runner = ?, updated_at = ? WHERE id = ?",
                (runner, utc_now(), job_id),
            )

    def set_job_status(self, job_id: str, status: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (status, utc_now(), job_id),
            )

    def rollback(self, snapshot_id: str) -> None:
        active = self.active_snapshot()
        with self.connection() as connection:
            target = connection.execute(
                "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if target is None:
                raise ValueError(f"Unknown snapshot: {snapshot_id}")
            if hash_tree(self.root / str(target["relative_path"])) != target["tree_hash"]:
                raise ValueError(f"Cannot activate corrupt snapshot: {snapshot_id}")
            connection.execute(
                "INSERT INTO activations(snapshot_id, previous_snapshot_id, reason, created_at) "
                "VALUES (?, ?, ?, ?)",
                (snapshot_id, active["id"], f"rollback:{snapshot_id}", utc_now()),
            )

    def history(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT a.id AS activation_id, a.snapshot_id, a.previous_snapshot_id, "
                "a.reason, a.created_at, s.tree_hash, rc.runner, rc.model, rc.duration_ms, "
                "rc.input_tokens, rc.output_tokens FROM activations a "
                "JOIN snapshots s ON s.id = a.snapshot_id "
                "LEFT JOIN runner_calls rc ON rc.id = (SELECT MAX(id) FROM runner_calls "
                "WHERE job_id = s.source_job_id) ORDER BY a.id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def record_answer(
        self, *, snapshot_id: str, question: str, answer: str, runner: str, model: str
    ) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO answers(snapshot_id, question, answer, runner, model, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (snapshot_id, question, answer, runner, model, utc_now()),
            )
            return int(cursor.lastrowid)

    def status(self) -> dict[str, Any]:
        active = self.active_snapshot()
        with self.connection() as connection:
            pending = connection.execute(
                "SELECT COUNT(*) FROM source_revisions sr JOIN "
                "(SELECT source_id, MAX(revision_no) revision_no FROM source_revisions "
                "GROUP BY source_id) current ON current.source_id = sr.source_id "
                "AND current.revision_no = sr.revision_no WHERE sr.applied_job_id IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM packets p "
                "WHERE p.note_source_revision_id = sr.id) "
                "AND NOT EXISTS (SELECT 1 FROM packet_items pi "
                "WHERE pi.source_revision_id = sr.id)"
            ).fetchone()[0]
            proposal = connection.execute(
                "SELECT id, status, current_revision FROM jobs "
                "WHERE status IN ('running', 'proposed') ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return {
            "active_snapshot": active["id"],
            "tree_hash": active["tree_hash"],
            "pending_sources": int(pending),
            "proposal": dict(proposal) if proposal else None,
        }

    def jobs(self, limit: int = 50, *, include_packets: bool = True) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT j.*, jp.packet_id FROM jobs j LEFT JOIN job_packets jp "
                "ON jp.job_id = j.id "
                + ("" if include_packets else "WHERE jp.packet_id IS NULL ")
                + "ORDER BY j.created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_proposal(self) -> dict[str, Any] | None:
        proposals = self.proposals(limit=1)
        return proposals[0] if proposals else None

    def proposals(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status = 'proposed' ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [self.proposal(str(row["id"])) for row in rows]

    def verify_active(self) -> None:
        snapshot = self.active_snapshot()
        actual = hash_tree(self.snapshot_path(snapshot))
        if actual != snapshot["tree_hash"]:
            raise ValueError(
                "Active Wiki was modified outside Memorex; restore it with rollback "
                "before continuing"
            )

    def list_pages(self, snapshot: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        current = snapshot or self.active_snapshot()
        wiki = self.snapshot_path(current) / "wiki"
        result = []
        for path in sorted(wiki.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            first = next(
                (line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path.stem
            )
            result.append({"slug": path.stem, "title": first, "text": text, "path": path})
        return result

    def reconcile_notes(self) -> None:
        """Register active Wiki pages as notes without changing existing organization."""
        pages = [page for page in self.list_pages() if page["slug"] != "README"]
        readme = next((page for page in self.list_pages() if page["slug"] == "README"), None)
        suggested: dict[str, str] = {}
        heading = ""
        with self.connection() as connection:
            import_legacy_readme = (
                connection.execute("SELECT 1 FROM notebooks WHERE system_key = 'inbox'").fetchone()
                is None
            )
        if readme and import_legacy_readme:
            for line in str(readme["text"]).splitlines():
                if line.startswith("## "):
                    heading = line[3:].strip()
                if heading:
                    for slug in WIKI_LINK.findall(line):
                        suggested.setdefault(slug, heading)
        now = utc_now()
        with self.connection() as connection:
            inbox = connection.execute(
                "SELECT id FROM notebooks WHERE system_key = 'inbox'"
            ).fetchone()
            if inbox is None:
                inbox_id = uuid.uuid4().hex[:12]
                connection.execute(
                    "INSERT INTO notebooks(id,name,system_key,created_at,updated_at) "
                    "VALUES (?,?,'inbox',?,?)",
                    (inbox_id, "Входящие", now, now),
                )
            else:
                inbox_id = str(inbox["id"])
            notebook_ids: dict[str, str] = {}
            for name in dict.fromkeys(suggested.values()):
                row = connection.execute(
                    "SELECT id FROM notebooks WHERE name = ?", (name,)
                ).fetchone()
                notebook_id = str(row["id"]) if row else uuid.uuid4().hex[:12]
                if row is None:
                    connection.execute(
                        "INSERT INTO notebooks(id,name,created_at,updated_at) VALUES (?,?,?,?)",
                        (notebook_id, name, now, now),
                    )
                notebook_ids[name] = notebook_id
            existing = {
                str(row["slug"]) for row in connection.execute("SELECT slug FROM notes").fetchall()
            }
            for page in pages:
                slug = str(page["slug"])
                if slug in existing:
                    continue
                notebook_id = notebook_ids.get(suggested.get(slug, ""), inbox_id)
                connection.execute(
                    "INSERT INTO notes(id,slug,notebook_id,created_at,updated_at) "
                    "VALUES (?,?,?,?,?)",
                    (uuid.uuid4().hex[:12], slug, notebook_id, now, now),
                )

    def notebooks(self) -> list[dict[str, Any]]:
        active = {str(page["slug"]) for page in self.list_pages() if page["slug"] != "README"}
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT nb.*, n.slug FROM notebooks nb LEFT JOIN notes n "
                "ON n.notebook_id = nb.id ORDER BY nb.system_key IS NULL, nb.name"
            ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            notebook = grouped.setdefault(
                str(row["id"]),
                {
                    "id": row["id"],
                    "name": row["name"],
                    "system_key": row["system_key"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "note_count": 0,
                },
            )
            if row["slug"] in active:
                notebook["note_count"] = int(notebook["note_count"]) + 1
        return list(grouped.values())

    def create_notebook(self, name: str) -> dict[str, Any]:
        clean = name.strip()
        if not clean or len(clean) > 120:
            raise ValueError("Notebook name must contain 1-120 characters")
        notebook_id = uuid.uuid4().hex[:12]
        now = utc_now()
        try:
            with self.connection() as connection:
                connection.execute(
                    "INSERT INTO notebooks(id,name,created_at,updated_at) VALUES (?,?,?,?)",
                    (notebook_id, clean, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Notebook already exists: {clean}") from exc
        return self.notebook(notebook_id)

    def notebook(self, notebook_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown notebook: {notebook_id}")
        return dict(row)

    def rename_notebook(self, notebook_id: str, name: str) -> None:
        notebook = self.notebook(notebook_id)
        if notebook["system_key"]:
            raise ValueError("The Inbox notebook cannot be renamed")
        clean = name.strip()
        if not clean or len(clean) > 120:
            raise ValueError("Notebook name must contain 1-120 characters")
        try:
            with self.connection() as connection:
                connection.execute(
                    "UPDATE notebooks SET name = ?, updated_at = ? WHERE id = ?",
                    (clean, utc_now(), notebook_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Notebook already exists: {clean}") from exc

    def delete_notebook(self, notebook_id: str) -> None:
        notebook = self.notebook(notebook_id)
        if notebook["system_key"]:
            raise ValueError("The Inbox notebook cannot be deleted")
        with self.connection() as connection:
            inbox = connection.execute(
                "SELECT id FROM notebooks WHERE system_key = 'inbox'"
            ).fetchone()
            connection.execute(
                "UPDATE notes SET notebook_id = ?, updated_at = ? WHERE notebook_id = ?",
                (inbox["id"], utc_now(), notebook_id),
            )
            connection.execute("DELETE FROM notebooks WHERE id = ?", (notebook_id,))

    def notes(self, notebook_id: str | None = None) -> list[dict[str, Any]]:
        pages = {str(page["slug"]): page for page in self.list_pages() if page["slug"] != "README"}
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT n.*, nb.name notebook_name FROM notes n JOIN notebooks nb "
                "ON nb.id = n.notebook_id "
                + ("WHERE n.notebook_id = ? " if notebook_id else "")
                + "ORDER BY n.updated_at DESC",
                (notebook_id,) if notebook_id else (),
            ).fetchall()
        return [
            {**dict(row), **pages[str(row["slug"])]} for row in rows if str(row["slug"]) in pages
        ]

    def note(self, note_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT n.*, nb.name notebook_name FROM notes n JOIN notebooks nb "
                "ON nb.id = n.notebook_id WHERE n.id = ?",
                (note_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown note: {note_id}")
        page = next((page for page in self.list_pages() if page["slug"] == row["slug"]), None)
        if page is None:
            raise ValueError(f"Note is not present in the active memory: {note_id}")
        return {**dict(row), **page, "attachments": self.note_attachments(note_id)}

    def note_for_slug(self, slug: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT id FROM notes WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown note slug: {slug}")
        return self.note(str(row["id"]))

    def move_note(self, note_id: str, notebook_id: str) -> None:
        self.notebook(notebook_id)
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE notes SET notebook_id = ?, updated_at = ? WHERE id = ?",
                (notebook_id, utc_now(), note_id),
            )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown note: {note_id}")

    def activate_manual_note(
        self,
        *,
        expected_snapshot_id: str,
        snapshot_id: str,
        relative_path: str,
        tree_hash: str,
        note_id: str,
        slug: str,
        notebook_id: str,
        new_note: bool,
    ) -> None:
        now = utc_now()
        with self.connection() as connection:
            active = connection.execute(
                "SELECT snapshot_id FROM activations ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if active is None or str(active["snapshot_id"]) != expected_snapshot_id:
                raise ValueError("The memory changed while this note was being edited")
            self.notebook(notebook_id)
            connection.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?, ?, NULL, ?)",
                (snapshot_id, expected_snapshot_id, relative_path, tree_hash, now),
            )
            connection.execute(
                "INSERT INTO activations(snapshot_id,previous_snapshot_id,reason,created_at) "
                "VALUES (?,?,?,?)",
                (
                    snapshot_id,
                    expected_snapshot_id,
                    f"manual-{'create' if new_note else 'edit'}:{note_id}",
                    now,
                ),
            )
            if new_note:
                connection.execute(
                    "INSERT INTO notes(id,slug,notebook_id,created_at,updated_at) "
                    "VALUES (?,?,?,?,?)",
                    (note_id, slug, notebook_id, now, now),
                )
            else:
                cursor = connection.execute(
                    "UPDATE notes SET notebook_id = ?, updated_at = ? WHERE id = ? AND slug = ?",
                    (notebook_id, now, note_id, slug),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"Unknown note: {note_id}")

    def add_note_attachment(
        self, note_id: str, *, name: str, mime_type: str, data: bytes
    ) -> dict[str, Any]:
        self.note(note_id)
        safe_name = Path(name).name
        if not safe_name or safe_name != name or "\\" in name:
            raise ValueError(f"Unsafe attachment filename: {name}")
        attachment_id = uuid.uuid4().hex[:12]
        canonical = f"note://{note_id}/{attachment_id}/{safe_name}"
        checksum, object_path, normalized_path = self._prepare_binary_bytes(
            data, canonical, mime_type
        )
        now = utc_now()
        with self.connection() as connection:
            revision = self._register_text(
                connection,
                canonical_path=canonical,
                kind="attachment",
                checksum=checksum,
                object_path=object_path,
                normalized_path=normalized_path,
            )
            ordinal = int(
                connection.execute(
                    "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM note_attachments WHERE note_id = ?",
                    (note_id,),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO note_attachments(id,note_id,source_revision_id,display_name,"
                "mime_type,ordinal,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    attachment_id,
                    note_id,
                    int(revision["id"]),
                    safe_name,
                    mime_type,
                    ordinal,
                    now,
                ),
            )
            connection.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (now, note_id))
        return self.note_attachment(note_id, attachment_id)

    def note_attachments(
        self, note_id: str, *, include_removed: bool = False
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT na.*, sr.object_path, sr.normalized_path, sr.sha256 "
                "FROM note_attachments na JOIN source_revisions sr "
                "ON sr.id = na.source_revision_id WHERE na.note_id = ? "
                + ("" if include_removed else "AND na.removed_at IS NULL ")
                + "ORDER BY na.ordinal",
                (note_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def note_attachment(self, note_id: str, attachment_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT na.*, sr.object_path, sr.normalized_path, sr.sha256 "
                "FROM note_attachments na JOIN source_revisions sr "
                "ON sr.id = na.source_revision_id WHERE na.note_id = ? AND na.id = ?",
                (note_id, attachment_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown note attachment: {attachment_id}")
        return dict(row)

    def remove_note_attachment(self, note_id: str, attachment_id: str) -> None:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE note_attachments SET removed_at = ? "
                "WHERE note_id = ? AND id = ? AND removed_at IS NULL",
                (utc_now(), note_id, attachment_id),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    "UPDATE notes SET updated_at = ? WHERE id = ?", (utc_now(), note_id)
                )
        if cursor.rowcount != 1:
            raise ValueError(f"Unknown active note attachment: {attachment_id}")

    def rebuild_fts(self) -> None:
        if not self.database_path.exists():
            return
        pages = self.list_pages()
        outbound = {str(p["slug"]): set(WIKI_LINK.findall(str(p["text"]))) for p in pages}
        inbound: dict[str, set[str]] = {str(p["slug"]): set() for p in pages}
        for source, targets in outbound.items():
            for target in targets:
                if target in inbound:
                    inbound[target].add(source)
        with self.connection() as connection:
            connection.execute("DELETE FROM wiki_fts")
            connection.executemany(
                "INSERT INTO wiki_fts(slug,title,text,links,backlinks) VALUES (?,?,?,?,?)",
                [
                    (
                        p["slug"],
                        p["title"],
                        p["text"],
                        " ".join(sorted(outbound[str(p["slug"])])),
                        " ".join(sorted(inbound[str(p["slug"])])),
                    )
                    for p in pages
                ],
            )

    def search_pages(self, text: str, *, limit: int, related_limit: int) -> list[str]:
        terms = []
        for term in WORD.findall(text.lower()):
            if term not in terms:
                terms.append(term)
        if not terms:
            return []
        query = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms[:24])
        with self.connection() as connection:
            try:
                rows = connection.execute(
                    "SELECT slug, links, backlinks FROM wiki_fts WHERE wiki_fts MATCH ? "
                    "ORDER BY bm25(wiki_fts) LIMIT ?",
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        selected = [str(row["slug"]) for row in rows]
        for row in rows:
            for slug in (str(row["links"]) + " " + str(row["backlinks"])).split():
                if slug not in selected and len(selected) < limit + related_limit:
                    selected.append(slug)
        return selected

    def search_notes(self, text: str, *, limit: int = 30) -> list[dict[str, Any]]:
        query_text = text.strip()
        if not query_text:
            return []
        terms = list(dict.fromkeys(WORD.findall(query_text.lower())))[:24]
        slugs: list[str] = []
        if terms:
            query = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms)
            with self.connection() as connection:
                try:
                    rows = connection.execute(
                        "SELECT slug FROM wiki_fts WHERE wiki_fts MATCH ? "
                        "ORDER BY bm25(wiki_fts) LIMIT ?",
                        (query, limit),
                    ).fetchall()
                    slugs = [str(row["slug"]) for row in rows if row["slug"] != "README"]
                except sqlite3.OperationalError:
                    slugs = []
        notes = self.notes()
        by_slug = {str(note["slug"]): note for note in notes}
        result = [by_slug[slug] for slug in slugs if slug in by_slug]
        folded = query_text.casefold()
        for note in notes:
            if note in result:
                continue
            if folded in (str(note["title"]) + "\n" + str(note["text"])).casefold():
                result.append(note)
            if len(result) >= limit:
                break
        for note in result:
            plain = " ".join(
                re.sub(r"^[# |\-`)]+", "", line).rstrip()
                for line in str(note["text"]).splitlines()[1:]
                if line.strip() and not line.startswith("## Источники")
            )
            note["snippet"] = plain[:240]
        return result[:limit]

    def add_task_event(
        self, task_id: str, phase: str, payload: dict[str, object], job_id: str | None = None
    ) -> None:
        safe = {
            key: value
            for key, value in payload.items()
            if key
            in {
                "phase",
                "runner",
                "model",
                "elapsed_ms",
                "fallback",
                "sources",
                "selected_pages",
                "selected_count",
                "total_pages",
                "message",
                "status",
                "job_id",
                "next",
            }
        }
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO task_events(task_id,job_id,phase,payload_json,created_at) "
                "VALUES (?,?,?,?,?)",
                (task_id, job_id, phase, json.dumps(safe, ensure_ascii=False), utc_now()),
            )

    def task_events(self, task_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM task_events WHERE task_id = ? AND id > ? ORDER BY id",
                (task_id, after),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def create_chat(self, title: str) -> str:
        session_id = uuid.uuid4().hex[:12]
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO chat_sessions VALUES (?,?,?,?)", (session_id, title[:120], now, now)
            )
        return session_id

    def create_discussion(self, title: str, note_ids: list[str]) -> dict[str, Any]:
        clean = title.strip() or "Новое обсуждение"
        unique = list(dict.fromkeys(note_ids))
        for note_id in unique:
            self.note(note_id)
        session_id = self.create_chat(clean)
        now = utc_now()
        with self.connection() as connection:
            connection.executemany(
                "INSERT INTO discussion_notes(session_id,note_id,ordinal,created_at) "
                "VALUES (?,?,?,?)",
                [(session_id, note_id, index, now) for index, note_id in enumerate(unique)],
            )
        return self.discussion(session_id)

    def discussion(self, session_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            session = connection.execute(
                "SELECT * FROM chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            rows = connection.execute(
                "SELECT note_id FROM discussion_notes WHERE session_id = ? ORDER BY ordinal",
                (session_id,),
            ).fetchall()
            turns = connection.execute(
                "SELECT * FROM discussion_turns WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        if session is None:
            raise ValueError(f"Unknown discussion: {session_id}")
        notes = []
        for row in rows:
            try:
                notes.append(self.note(str(row["note_id"])))
            except ValueError:
                continue
        return {**dict(session), "notes": notes, "turns": [dict(row) for row in turns]}

    def set_discussion_notes(self, session_id: str, note_ids: list[str]) -> None:
        self.discussion(session_id)
        unique = list(dict.fromkeys(note_ids))
        for note_id in unique:
            self.note(note_id)
        now = utc_now()
        with self.connection() as connection:
            connection.execute("DELETE FROM discussion_notes WHERE session_id = ?", (session_id,))
            connection.executemany(
                "INSERT INTO discussion_notes(session_id,note_id,ordinal,created_at) "
                "VALUES (?,?,?,?)",
                [(session_id, note_id, index, now) for index, note_id in enumerate(unique)],
            )

    def create_discussion_turn(self, session_id: str, question: str) -> dict[str, Any]:
        if not question.strip():
            raise ValueError("Discussion question must not be empty")
        discussion = self.discussion(session_id)
        if not discussion["notes"]:
            raise ValueError("Choose at least one note for the discussion")
        snapshot = self.active_snapshot()
        turn_id = uuid.uuid4().hex[:12]
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO chat_messages(session_id,role,content,snapshot_id,created_at) "
                "VALUES (?, 'user', ?, ?, ?)",
                (session_id, question, snapshot["id"], now),
            )
            message_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO discussion_turns(id,session_id,question_message_id,status,"
                "created_at,updated_at) VALUES (?,?,?,'pending',?,?)",
                (turn_id, session_id, message_id, now, now),
            )
            connection.executemany(
                "INSERT INTO discussion_turn_notes(turn_id,note_id,snapshot_id,ordinal) "
                "VALUES (?,?,?,?)",
                [
                    (turn_id, note["id"], snapshot["id"], index)
                    for index, note in enumerate(discussion["notes"])
                ],
            )
            connection.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (now, session_id)
            )
        return self.discussion_turn(turn_id)

    def discussion_turn(self, turn_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT dt.*, cm.content question FROM discussion_turns dt "
                "JOIN chat_messages cm ON cm.id = dt.question_message_id WHERE dt.id = ?",
                (turn_id,),
            ).fetchone()
            note_rows = connection.execute(
                "SELECT note_id,snapshot_id FROM discussion_turn_notes "
                "WHERE turn_id = ? ORDER BY ordinal",
                (turn_id,),
            ).fetchall()
        if row is None:
            raise ValueError(f"Unknown discussion turn: {turn_id}")
        return {
            **dict(row),
            "note_ids": [str(item["note_id"]) for item in note_rows],
            "snapshot_id": str(note_rows[0]["snapshot_id"]) if note_rows else None,
        }

    def start_discussion_turn(self, turn_id: str, task_id: str) -> None:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE discussion_turns SET status='running',task_id=?,error=NULL,updated_at=? "
                "WHERE id=? AND status IN ('pending','failed')",
                (task_id, utc_now(), turn_id),
            )
        if cursor.rowcount != 1:
            raise ValueError(f"Discussion turn is not runnable: {turn_id}")

    def finish_discussion_turn(self, turn_id: str, answer: str, *, runner: str, model: str) -> int:
        turn = self.discussion_turn(turn_id)
        now = utc_now()
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO chat_messages(session_id,role,content,snapshot_id,runner,model,"
                "created_at) VALUES (?,'assistant',?,?,?,?,?)",
                (turn["session_id"], answer, turn["snapshot_id"], runner, model, now),
            )
            answer_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE discussion_turns SET answer_message_id=?,status='succeeded',"
                "error=NULL,updated_at=? WHERE id=?",
                (answer_id, now, turn_id),
            )
            connection.execute(
                "UPDATE chat_sessions SET updated_at=? WHERE id=?",
                (now, turn["session_id"]),
            )
        return answer_id

    def fail_discussion_turn(self, turn_id: str, error: str, status: str = "failed") -> None:
        if status not in {"failed", "cancelled"}:
            raise ValueError(f"Invalid discussion failure status: {status}")
        with self.connection() as connection:
            connection.execute(
                "UPDATE discussion_turns SET status=?,error=?,updated_at=? WHERE id=?",
                (status, error, utc_now(), turn_id),
            )

    def recover_interrupted_discussions(self) -> int:
        if not self.database_path.exists():
            return 0
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE discussion_turns SET status='failed',error='interrupted; retry available',"
                "updated_at=? WHERE status IN ('pending','running')",
                (utc_now(),),
            )
        return int(cursor.rowcount)

    def add_chat_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        snapshot_id: str | None = None,
        runner: str | None = None,
        model: str | None = None,
    ) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO chat_messages(session_id,role,content,snapshot_id,runner,model,"
                "created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (session_id, role, content, snapshot_id, runner, model, utc_now()),
            )
            connection.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (utc_now(), session_id)
            )
            return int(cursor.lastrowid)

    def chat_sessions(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM chat_sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def chat_messages(self, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM (SELECT * FROM chat_messages WHERE session_id = ? "
                "ORDER BY id DESC LIMIT ?) "
                "ORDER BY id",
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def chat_message(self, message_id: int) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM chat_messages WHERE id = ?", (message_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown discussion message: {message_id}")
        return dict(row)

    def sync_vault(self) -> Path:
        snapshot = self.active_snapshot()
        source = self.snapshot_path(snapshot)
        vault = self.settings.root / "vault"
        staging = self.settings.root / f".vault-next-{uuid.uuid4().hex[:8]}"
        shutil.copytree(source, staging)
        make_tree_read_only(staging)
        old = self.settings.root / f".vault-old-{uuid.uuid4().hex[:8]}"
        if vault.exists():
            make_tree_writable(vault)
            vault.rename(old)
        staging.rename(vault)
        if old.exists():
            shutil.rmtree(old)
        return vault

    def _prepare_text_bytes(self, data: bytes, label: str) -> tuple[str, Path, Path]:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Source is not UTF-8: {label}") from exc
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").encode()
        checksum = hashlib.sha256(data).hexdigest()
        normalized_checksum = hashlib.sha256(normalized).hexdigest()
        return (
            checksum,
            self._store_bytes(data, checksum, "raw"),
            self._store_bytes(normalized, normalized_checksum, "normalized"),
        )

    def _prepare_image_bytes(
        self, data: bytes, label: str, mime_type: str
    ) -> tuple[str, Path, Path]:
        checksum = hashlib.sha256(data).hexdigest()
        metadata = (
            "# Изображение\n\n"
            f"- Имя: {Path(label).name}\n"
            f"- MIME: {mime_type}\n"
            f"- SHA-256: {checksum}\n"
        ).encode()
        metadata_checksum = hashlib.sha256(metadata).hexdigest()
        return (
            checksum,
            self._store_bytes(data, checksum, "raw"),
            self._store_bytes(metadata, metadata_checksum, "normalized"),
        )

    def _prepare_binary_bytes(
        self, data: bytes, label: str, mime_type: str
    ) -> tuple[str, Path, Path]:
        checksum = hashlib.sha256(data).hexdigest()
        metadata = (
            "# Вложение\n\n"
            f"- Имя: {Path(label).name}\n"
            f"- MIME: {mime_type}\n"
            f"- Размер: {len(data)} bytes\n"
            f"- SHA-256: {checksum}\n"
        ).encode()
        metadata_checksum = hashlib.sha256(metadata).hexdigest()
        return (
            checksum,
            self._store_bytes(data, checksum, "raw"),
            self._store_bytes(metadata, metadata_checksum, "normalized"),
        )

    def _store_bytes(self, data: bytes, checksum: str, namespace: str) -> Path:
        target = self.objects_dir / namespace / checksum[:2] / checksum
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != checksum:
                raise ValueError(f"Corrupt immutable object: {target}")
            return target
        target.write_bytes(data)
        target.chmod(0o444)
        return target

    def _ensure_runner_call_columns(self, connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(runner_calls)").fetchall()
        }
        for name in ("input_tokens", "output_tokens", "cached_input_tokens"):
            if name not in columns:
                connection.execute(f"ALTER TABLE runner_calls ADD COLUMN {name} INTEGER")
        for name in ("profile", "effort"):
            if name not in columns:
                connection.execute(f"ALTER TABLE runner_calls ADD COLUMN {name} TEXT")

    def _apply_migrations(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        applied = {
            int(row["version"])
            for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
        }
        for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_wiki_first_*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version not in applied:
                connection.executescript(path.read_text(encoding="utf-8"))

    def _ensure_proposal_columns(self, connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(proposal_revisions)")
        }
        if "retrieval_json" not in columns:
            connection.execute(
                "ALTER TABLE proposal_revisions ADD COLUMN retrieval_json "
                "TEXT NOT NULL DEFAULT '{}'"
            )


def hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def make_tree_read_only(root: Path) -> None:
    for path in root.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def _image_mime_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("Unsupported or corrupt image content")


def make_tree_writable(root: Path) -> None:
    root.chmod(0o755)
    for path in root.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
