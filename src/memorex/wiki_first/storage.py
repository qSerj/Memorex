from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memorex.config import WorkspaceSettings

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
"""


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
            self._ensure_runner_call_columns(connection)
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
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Source is not UTF-8: {path}") from exc
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        normalized_data = normalized.encode("utf-8")
        checksum = hashlib.sha256(data).hexdigest()
        normalized_checksum = hashlib.sha256(normalized_data).hexdigest()
        object_path = self._store_bytes(data, checksum, "raw")
        normalized_path = self._store_bytes(normalized_data, normalized_checksum, "normalized")
        canonical = str(path.resolve())
        now = utc_now()
        with self.connection() as connection:
            source = connection.execute(
                "SELECT * FROM sources WHERE canonical_path = ?", (canonical,)
            ).fetchone()
            if source is None:
                cursor = connection.execute(
                    "INSERT INTO sources(canonical_path, kind, created_at) VALUES (?, ?, ?)",
                    (canonical, kind, now),
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
                    return {**dict(current), "status": "unchanged", "canonical_path": canonical}
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
            "canonical_path": canonical,
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

    def pending_revisions(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT sr.*, s.canonical_path, s.kind FROM source_revisions sr "
                "JOIN sources s ON s.id = sr.source_id "
                "JOIN (SELECT source_id, MAX(revision_no) revision_no FROM source_revisions "
                "GROUP BY source_id) current ON current.source_id = sr.source_id "
                "AND current.revision_no = sr.revision_no WHERE sr.applied_job_id IS NULL "
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
    ) -> dict[str, Any]:
        base = self.active_snapshot()
        now = utc_now()
        with self.connection() as connection:
            unfinished = connection.execute(
                "SELECT id FROM jobs WHERE status IN ('running', 'proposed') LIMIT 1"
            ).fetchone()
            if unfinished is not None:
                raise ValueError(f"Finish or reject proposal {unfinished['id']} first")
            connection.execute(
                "INSERT INTO jobs(id, kind, status, base_snapshot_id, runner, user_input, "
                "created_at, updated_at) VALUES (?, ?, 'running', ?, ?, ?, ?, ?)",
                (job_id, kind, base["id"], runner, user_input, now, now),
            )
            connection.executemany(
                "INSERT INTO job_sources(job_id, source_revision_id) VALUES (?, ?)",
                [(job_id, revision_id) for revision_id in source_revision_ids],
            )
        return self.get_job(job_id)

    def recover_interrupted_jobs(self) -> int:
        """Fail synchronous jobs left running by a terminated CLI process."""
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = 'failed', "
                "rejection_reason = 'interrupted CLI process', updated_at = ? "
                "WHERE status = 'running'",
                (utc_now(),),
            )
            return int(cursor.rowcount)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
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
    ) -> None:
        now = utc_now()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO proposal_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, revision_no, relative_path, tree_hash, feedback, validation_json, now),
            )
            connection.execute(
                "UPDATE jobs SET status = 'proposed', current_revision = ?, updated_at = ? "
                "WHERE id = ?",
                (revision_no, now, job_id),
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
                "output_tokens, cached_input_tokens, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    utc_now(),
                ),
            )

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

    def reject(self, job_id: str, reason: str) -> None:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = 'rejected', rejection_reason = ?, updated_at = ? "
                "WHERE id = ? AND status = 'proposed'",
                (reason, utc_now(), job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Proposal {job_id} is not awaiting review")

    def fail_job(self, job_id: str, reason: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'failed', rejection_reason = ?, updated_at = ? "
                "WHERE id = ?",
                (reason, utc_now(), job_id),
            )

    def set_job_runner(self, job_id: str, runner: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE jobs SET runner = ?, updated_at = ? WHERE id = ?",
                (runner, utc_now(), job_id),
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
                "a.reason, a.created_at, s.tree_hash FROM activations a "
                "JOIN snapshots s ON s.id = a.snapshot_id ORDER BY a.id DESC"
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
                "AND current.revision_no = sr.revision_no WHERE sr.applied_job_id IS NULL"
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

    def verify_active(self) -> None:
        snapshot = self.active_snapshot()
        actual = hash_tree(self.snapshot_path(snapshot))
        if actual != snapshot["tree_hash"]:
            raise ValueError(
                "Active Wiki was modified outside Memorex; restore it with rollback "
                "before continuing"
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
