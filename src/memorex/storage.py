from __future__ import annotations

import hashlib
import importlib.resources
import os
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memorex.config import WorkspaceConfig
from memorex.ingest import PARSER_VERSION, ParsedSource


class WorkspaceNotInitialized(RuntimeError):
    """Raised when a command needs an initialized workspace."""


class RecordNotFound(LookupError):
    """Raised when a requested source or claim does not exist."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Storage:
    """Owns SQLite and immutable object-store persistence."""

    def __init__(self, config: WorkspaceConfig) -> None:
        self.config = config

    def initialize(self) -> dict[str, Any]:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.config.objects_dir.mkdir(parents=True, exist_ok=True)
        with self._connection(require_initialized=False, transactional=True) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations")
            }
            migration_root = importlib.resources.files("memorex.migrations")
            for migration in sorted(migration_root.iterdir(), key=lambda item: item.name):
                if migration.suffix != ".sql":
                    continue
                version = int(migration.name.split("_", 1)[0])
                if version in applied:
                    continue
                script = migration.read_text(encoding="utf-8")
                applied_at = utc_now().replace("'", "''")
                connection.executescript(
                    f"BEGIN IMMEDIATE;\n{script}\n"
                    f"INSERT INTO schema_migrations(version, applied_at) "
                    f"VALUES ({version}, '{applied_at}');\nCOMMIT;"
                )
            connection.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(body)")
        return {
            "status": "initialized",
            "data_dir": str(self.config.data_dir),
            "database": str(self.config.database_path),
            "fts5": True,
        }

    def ingest_source(self, parsed: ParsedSource) -> dict[str, Any]:
        object_path = self._store_object(parsed.data, parsed.sha256)
        relative_path = object_path.relative_to(self.config.data_dir).as_posix()
        now = utc_now()
        with self._connection(transactional=True) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO objects(sha256, relative_path, size_bytes, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (parsed.sha256, relative_path, len(parsed.data), now),
            )
            source = connection.execute(
                "SELECT id, current_version_id FROM sources WHERE canonical_path = ?",
                (str(parsed.path),),
            ).fetchone()
            if source is not None and source["current_version_id"] is not None:
                current = connection.execute(
                    "SELECT id, revision_no, object_sha256 FROM source_versions WHERE id = ?",
                    (source["current_version_id"],),
                ).fetchone()
                if current is not None and current["object_sha256"] == parsed.sha256:
                    connection.execute(
                        "UPDATE sources SET last_seen_at = ? WHERE id = ?", (now, source["id"])
                    )
                    return {
                        "status": "unchanged",
                        "source_id": source["id"],
                        "version_id": current["id"],
                        "revision": current["revision_no"],
                        "sha256": parsed.sha256,
                        "segments": len(parsed.segments),
                        "object_path": relative_path,
                    }

            if source is None:
                cursor = connection.execute(
                    """
                    INSERT INTO sources(canonical_path, created_at, last_seen_at)
                    VALUES (?, ?, ?)
                    """,
                    (str(parsed.path), now, now),
                )
                source_id = cursor.lastrowid
                revision = 1
                result_status = "added"
            else:
                source_id = source["id"]
                revision = connection.execute(
                    """
                    SELECT COALESCE(MAX(revision_no), 0) + 1
                    FROM source_versions
                    WHERE source_id = ?
                    """,
                    (source_id,),
                ).fetchone()[0]
                connection.execute(
                    "UPDATE sources SET last_seen_at = ? WHERE id = ?", (now, source_id)
                )
                result_status = "revised"

            cursor = connection.execute(
                """
                INSERT INTO source_versions(
                    source_id, revision_no, object_sha256, mime_type, size_bytes,
                    observed_at, status, parser_name, parser_version, normalized_text
                ) VALUES (?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?)
                """,
                (
                    source_id,
                    revision,
                    parsed.sha256,
                    parsed.mime_type,
                    len(parsed.data),
                    now,
                    "markdown" if parsed.mime_type == "text/markdown" else "plain_text",
                    PARSER_VERSION,
                    parsed.normalized_text,
                ),
            )
            version_id = cursor.lastrowid
            connection.executemany(
                """
                INSERT INTO segments(
                    source_version_id, ordinal, text, section, char_start, char_end
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        version_id,
                        segment.ordinal,
                        segment.text,
                        segment.section,
                        segment.char_start,
                        segment.char_end,
                    )
                    for segment in parsed.segments
                ],
            )
            connection.execute(
                "UPDATE sources SET current_version_id = ? WHERE id = ?",
                (version_id, source_id),
            )
        return {
            "status": result_status,
            "source_id": source_id,
            "version_id": version_id,
            "revision": revision,
            "sha256": parsed.sha256,
            "segments": len(parsed.segments),
            "object_path": relative_path,
        }

    def list_sources(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT s.id, s.canonical_path, s.created_at, s.last_seen_at,
                       sv.id AS version_id, sv.revision_no AS revision,
                       sv.object_sha256 AS sha256, sv.status,
                       COUNT(seg.id) AS segment_count
                FROM sources s
                JOIN source_versions sv ON sv.id = s.current_version_id
                LEFT JOIN segments seg ON seg.source_version_id = sv.id
                GROUP BY s.id, sv.id
                ORDER BY s.id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_source(self, source_id: int) -> dict[str, Any]:
        with self._connection() as connection:
            source = connection.execute(
                "SELECT * FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
            if source is None:
                raise RecordNotFound(f"Source {source_id} does not exist")
            versions = connection.execute(
                """
                SELECT sv.id, sv.revision_no AS revision, sv.object_sha256 AS sha256,
                       sv.mime_type, sv.size_bytes, sv.observed_at, sv.status,
                       sv.parser_name, sv.parser_version, sv.active_job_id,
                       COUNT(seg.id) AS segment_count
                FROM source_versions sv
                LEFT JOIN segments seg ON seg.source_version_id = sv.id
                WHERE sv.source_id = ?
                GROUP BY sv.id
                ORDER BY sv.revision_no DESC
                """,
                (source_id,),
            ).fetchall()
        result = dict(source)
        result["versions"] = [dict(row) for row in versions]
        return result

    def get_current_version(self, source_id: int) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT sv.*, s.canonical_path
                FROM sources s
                JOIN source_versions sv ON sv.id = s.current_version_id
                WHERE s.id = ?
                """,
                (source_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound(f"Source {source_id} does not exist")
        return dict(row)

    def get_segments(self, source_version_id: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM segments WHERE source_version_id = ? ORDER BY ordinal",
                (source_version_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_successful_job(
        self, source_version_id: int, model: str, base_url: str, prompt_version: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT j.* FROM jobs j
                JOIN source_versions sv ON sv.id = j.source_version_id
                WHERE j.source_version_id = ? AND j.model = ? AND j.base_url = ?
                  AND j.prompt_version = ? AND j.status = 'succeeded'
                  AND sv.active_job_id = j.id
                ORDER BY j.id DESC LIMIT 1
                """,
                (source_version_id, model, base_url, prompt_version),
            ).fetchone()
        return dict(row) if row else None

    def start_job(
        self, source_version_id: int, model: str, base_url: str, prompt_version: str
    ) -> int:
        with self._connection(transactional=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO jobs(
                    kind, source_version_id, model, base_url, prompt_version, status, started_at
                ) VALUES ('extract', ?, ?, ?, ?, 'running', ?)
                """,
                (source_version_id, model, base_url, prompt_version, utc_now()),
            )
        return int(cursor.lastrowid)

    def record_llm_call(
        self,
        *,
        job_id: int | None,
        purpose: str,
        segment_id: int | None,
        attempt: int,
        model: str,
        base_url: str,
        prompt_version: str,
        started_at: str,
        duration_ms: int,
        status: str,
        validation_valid: bool,
        input_tokens: int | None,
        output_tokens: int | None,
        error: str | None,
        raw_output: str | None,
    ) -> None:
        with self._connection(transactional=True) as connection:
            connection.execute(
                """
                INSERT INTO llm_calls(
                    job_id, purpose, segment_id, attempt, model, base_url, prompt_version,
                    started_at, duration_ms, status, validation_valid, input_tokens,
                    output_tokens, error, raw_output
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    purpose,
                    segment_id,
                    attempt,
                    model,
                    base_url,
                    prompt_version,
                    started_at,
                    duration_ms,
                    status,
                    int(validation_valid),
                    input_tokens,
                    output_tokens,
                    error,
                    raw_output,
                ),
            )

    def finish_job_success(
        self, job_id: int, source_version_id: int, claims: Iterable[dict[str, Any]]
    ) -> int:
        unique_claims: dict[tuple[str, int, int, int], dict[str, Any]] = {}
        for claim in claims:
            key = (
                claim["normalized_statement"],
                claim["segment_id"],
                claim["char_start"],
                claim["char_end"],
            )
            unique_claims.setdefault(key, claim)
        now = utc_now()
        with self._connection(transactional=True) as connection:
            for claim in unique_claims.values():
                cursor = connection.execute(
                    """
                    INSERT INTO claims(
                        job_id, source_version_id, statement, normalized_statement,
                        confidence, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'stated', ?)
                    """,
                    (
                        job_id,
                        source_version_id,
                        claim["statement"],
                        claim["normalized_statement"],
                        claim["confidence"],
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO claim_evidence(
                        claim_id, segment_id, quote, char_start, char_end
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        cursor.lastrowid,
                        claim["segment_id"],
                        claim["quote"],
                        claim["char_start"],
                        claim["char_end"],
                    ),
                )
            connection.execute(
                "UPDATE jobs SET status = 'succeeded', finished_at = ? WHERE id = ?",
                (now, job_id),
            )
            connection.execute(
                "UPDATE source_versions SET active_job_id = ? WHERE id = ?",
                (job_id, source_version_id),
            )
        return len(unique_claims)

    def finish_job_failure(self, job_id: int, error: str) -> None:
        with self._connection(transactional=True) as connection:
            connection.execute(
                "UPDATE jobs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                (utc_now(), error, job_id),
            )

    def list_claims(self, source_id: int | None = None) -> list[dict[str, Any]]:
        parameters: list[Any] = []
        condition = ""
        if source_id is not None:
            condition = "AND s.id = ?"
            parameters.append(source_id)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT c.id, c.statement, c.confidence, c.status, c.created_at,
                       s.id AS source_id, s.canonical_path, sv.revision_no AS revision,
                       ce.id AS evidence_id, ce.quote, ce.char_start, ce.char_end
                FROM claims c
                JOIN jobs j ON j.id = c.job_id AND j.status = 'succeeded'
                JOIN source_versions sv ON sv.id = c.source_version_id
                JOIN sources s ON s.current_version_id = sv.id
                JOIN claim_evidence ce ON ce.claim_id = c.id
                WHERE sv.active_job_id = j.id {condition}
                ORDER BY c.id
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_claim(self, claim_id: int) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT c.id, c.statement, c.normalized_statement, c.confidence,
                       c.status, c.created_at, c.job_id,
                       s.id AS source_id, s.canonical_path,
                       sv.id AS version_id, sv.revision_no AS revision,
                       ce.id AS evidence_id, ce.quote, ce.char_start, ce.char_end,
                       seg.id AS segment_id, seg.ordinal AS segment_ordinal, seg.section
                FROM claims c
                JOIN source_versions sv ON sv.id = c.source_version_id
                JOIN sources s ON s.id = sv.source_id
                JOIN claim_evidence ce ON ce.claim_id = c.id
                JOIN segments seg ON seg.id = ce.segment_id
                WHERE c.id = ?
                """,
                (claim_id,),
            ).fetchone()
            if row is None:
                raise RecordNotFound(f"Claim {claim_id} does not exist")
        return dict(row)

    def search_claims(self, fts_query: str, limit: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.statement, c.confidence,
                       s.id AS source_id, s.canonical_path,
                       sv.id AS version_id, sv.revision_no AS revision,
                       ce.id AS evidence_id, ce.quote, ce.char_start, ce.char_end,
                       bm25(claims_fts) AS rank
                FROM claims_fts
                JOIN claims c ON c.id = claims_fts.rowid
                JOIN jobs j ON j.id = c.job_id AND j.status = 'succeeded'
                JOIN source_versions sv ON sv.id = c.source_version_id
                JOIN sources s ON s.current_version_id = sv.id
                JOIN claim_evidence ce ON ce.claim_id = c.id
                WHERE claims_fts MATCH ? AND sv.active_job_id = j.id
                ORDER BY rank, c.id
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def _store_object(self, data: bytes, sha256: str) -> Path:
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha256:
            raise ValueError("Object checksum does not match supplied sha256")
        bucket = self.config.objects_dir / sha256[:2]
        bucket.mkdir(parents=True, exist_ok=True)
        target = bucket / sha256
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != sha256:
                raise RuntimeError(f"Object-store corruption detected: {target}")
            return target

        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{sha256}.", dir=bucket)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o444)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    @contextmanager
    def _connection(
        self, *, require_initialized: bool = True, transactional: bool = False
    ) -> Iterator[sqlite3.Connection]:
        if require_initialized and not self.config.database_path.is_file():
            raise WorkspaceNotInitialized(
                f"Workspace is not initialized at {self.config.data_dir}; run 'memorex init'"
            )
        connection = sqlite3.connect(self.config.database_path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 5000")
            if transactional:
                with connection:
                    yield connection
            else:
                yield connection
        finally:
            connection.close()
