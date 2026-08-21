from __future__ import annotations

import hashlib
import importlib.resources
import json
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
        self,
        job_id: int,
        source_version_id: int,
        claims: Iterable[dict[str, Any]],
        *,
        summary: dict[str, Any] | None = None,
        proposals: Iterable[dict[str, Any]] = (),
    ) -> int:
        claim_list = list(claims)
        unique_claims: dict[tuple[str, int, int, int], dict[str, Any]] = {}
        input_indices: dict[tuple[str, int, int, int], list[int]] = {}
        for index, claim in enumerate(claim_list):
            key = (
                claim["normalized_statement"],
                claim["segment_id"],
                claim["char_start"],
                claim["char_end"],
            )
            unique_claims.setdefault(key, claim)
            input_indices.setdefault(key, []).append(index)
        now = utc_now()
        claim_id_by_input_index: dict[int, int] = {}
        with self._connection(transactional=True) as connection:
            for key, claim in unique_claims.items():
                cursor = connection.execute(
                    """
                    INSERT INTO claims(
                        job_id, source_version_id, statement, normalized_statement,
                        confidence, status, created_at, kind, lifecycle, polarity,
                        actor, valid_from, valid_to
                    ) VALUES (?, ?, ?, ?, ?, 'stated', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        source_version_id,
                        claim["statement"],
                        claim["normalized_statement"],
                        claim["confidence"],
                        now,
                        claim.get("kind", "observation"),
                        claim.get("lifecycle", "unknown"),
                        claim.get("polarity", "positive"),
                        claim.get("actor"),
                        claim.get("valid_from"),
                        claim.get("valid_to"),
                    ),
                )
                claim_id = int(cursor.lastrowid)
                for input_index in input_indices[key]:
                    claim_id_by_input_index[input_index] = claim_id
                connection.execute(
                    """
                    INSERT INTO claim_evidence(
                        claim_id, segment_id, quote, char_start, char_end
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        claim_id,
                        claim["segment_id"],
                        claim["quote"],
                        claim["char_start"],
                        claim["char_end"],
                    ),
                )
                entity_ids: dict[tuple[str, str], int] = {}
                for entity in claim.get("entities", []):
                    entity_id = self._upsert_entity(connection, entity, now)
                    entity_ids[(entity["name"], entity["entity_type"])] = entity_id
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO claim_entities(claim_id, entity_id, role)
                        VALUES (?, ?, ?)
                        """,
                        (claim_id, entity_id, entity["role"]),
                    )
                for relation in claim.get("relations", []):
                    source_key = (relation["source_name"], relation["source_type"])
                    target_key = (relation["target_name"], relation["target_type"])
                    source_entity_id = entity_ids.get(source_key) or self._upsert_entity(
                        connection,
                        {
                            "name": relation["source_name"],
                            "entity_type": relation["source_type"],
                        },
                        now,
                    )
                    target_entity_id = entity_ids.get(target_key) or self._upsert_entity(
                        connection,
                        {
                            "name": relation["target_name"],
                            "entity_type": relation["target_type"],
                        },
                        now,
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO relations(
                            source_entity_id, predicate, target_entity_id,
                            evidence_claim_id, confidence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source_entity_id,
                            relation["predicate"],
                            target_entity_id,
                            claim_id,
                            relation["confidence"],
                            now,
                        ),
                    )
            if summary is not None:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO source_summaries(
                        source_version_id, job_id, title, body, prompt_version, model, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_version_id,
                        job_id,
                        summary["title"],
                        summary["body"],
                        summary["prompt_version"],
                        summary["model"],
                        now,
                    ),
                )
            for proposal in proposals:
                index = proposal["new_claim_index"]
                claim_id = claim_id_by_input_index.get(index)
                if claim_id is None:
                    continue
                connection.execute(
                    """
                    INSERT OR IGNORE INTO review_proposals(
                        proposal_type, subject_type, subject_id, object_type, object_id,
                        rationale, confidence, created_at
                    ) VALUES (?, 'claim', ?, 'claim', ?, ?, ?, ?)
                    """,
                    (
                        proposal["proposal_type"],
                        claim_id,
                        proposal["existing_claim_id"],
                        proposal["rationale"],
                        proposal["confidence"],
                        now,
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

    @staticmethod
    def _upsert_entity(
        connection: sqlite3.Connection, entity: dict[str, Any], created_at: str
    ) -> int:
        normalized = " ".join(entity["name"].casefold().split())
        connection.execute(
            """
            INSERT OR IGNORE INTO entities(
                canonical_name, normalized_name, entity_type, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (entity["name"], normalized, entity["entity_type"], created_at),
        )
        row = connection.execute(
            "SELECT id FROM entities WHERE normalized_name = ? AND entity_type = ?",
            (normalized, entity["entity_type"]),
        ).fetchone()
        if row is None:
            raise RuntimeError("Failed to resolve persisted entity")
        return int(row["id"])

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
                       c.kind, c.lifecycle, c.polarity, c.actor,
                       c.valid_from, c.valid_to, c.review_status,
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
                       c.status, c.created_at, c.job_id, c.kind, c.lifecycle,
                       c.polarity, c.actor, c.valid_from, c.valid_to, c.review_status,
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

    def get_evidence_context(self, claim_id: int, radius: int = 500) -> dict[str, Any]:
        claim = self.get_claim(claim_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT normalized_text FROM source_versions WHERE id = ?",
                (claim["version_id"],),
            ).fetchone()
        if row is None:
            raise RecordNotFound(f"Source version {claim['version_id']} does not exist")
        text = row["normalized_text"]
        context_start = max(0, claim["char_start"] - radius)
        context_end = min(len(text), claim["char_end"] + radius)
        return {
            **claim,
            "context_before": text[context_start : claim["char_start"]],
            "context_after": text[claim["char_end"] : context_end],
            "context_start": context_start,
            "context_end": context_end,
        }

    def search_claims(self, fts_query: str, limit: int) -> list[dict[str, Any]]:
        with self._connection() as connection:
            claim_rows = connection.execute(
                """
                SELECT c.id, c.statement, c.confidence, c.kind, c.lifecycle,
                       c.review_status, 0 AS is_override, NULL AS override_reason,
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
                  AND c.review_status IN ('machine', 'confirmed')
                  AND NOT EXISTS (
                      SELECT 1 FROM claim_links cl
                      WHERE cl.target_claim_id = c.id AND cl.relation_type = 'supersedes'
                  )
                ORDER BY rank, c.id
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
            override_rows = connection.execute(
                """
                SELECT c.id, u.statement, c.confidence, u.kind, u.lifecycle,
                       c.review_status, 1 AS is_override, u.reason AS override_reason,
                       s.id AS source_id, s.canonical_path,
                       sv.id AS version_id, sv.revision_no AS revision,
                       ce.id AS evidence_id, ce.quote, ce.char_start, ce.char_end,
                       bm25(user_overrides_fts) AS rank
                FROM user_overrides_fts
                JOIN user_overrides u ON u.id = user_overrides_fts.rowid
                JOIN claims c ON c.id = u.target_claim_id
                JOIN jobs j ON j.id = c.job_id AND j.status = 'succeeded'
                JOIN source_versions sv ON sv.id = c.source_version_id
                JOIN sources s ON s.current_version_id = sv.id
                JOIN claim_evidence ce ON ce.claim_id = c.id
                WHERE user_overrides_fts MATCH ? AND sv.active_job_id = j.id
                  AND c.review_status = 'overridden'
                  AND NOT EXISTS (
                      SELECT 1 FROM claim_links cl
                      WHERE cl.target_claim_id = c.id AND cl.relation_type = 'supersedes'
                  )
                  AND u.id = (
                      SELECT MAX(u2.id) FROM user_overrides u2
                      WHERE u2.target_claim_id = c.id
                  )
                ORDER BY rank, c.id
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
        combined = [dict(row) for row in claim_rows] + [dict(row) for row in override_rows]
        combined.sort(key=lambda row: (row["rank"], row["id"]))
        return combined[:limit]

    def stage_inbox_file(
        self, path: Path, *, sha256: str, size_bytes: int, mtime_ns: int
    ) -> dict[str, Any]:
        canonical_path = str(path.expanduser().resolve())
        now = utc_now()
        with self._connection(transactional=True) as connection:
            existing = connection.execute(
                "SELECT * FROM inbox_entries WHERE canonical_path = ?", (canonical_path,)
            ).fetchone()
            if existing is None:
                relocation_candidates = [
                    row
                    for row in connection.execute(
                        """
                        SELECT * FROM inbox_entries
                        WHERE sha256 = ? AND source_id IS NOT NULL
                        """,
                        (sha256,),
                    ).fetchall()
                    if not Path(row["canonical_path"]).exists()
                ]
                if len(relocation_candidates) == 1:
                    relocated = relocation_candidates[0]
                    connection.execute(
                        """
                        UPDATE inbox_entries
                        SET canonical_path = ?, size_bytes = ?, mtime_ns = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (canonical_path, size_bytes, mtime_ns, now, relocated["id"]),
                    )
                    connection.execute(
                        "UPDATE sources SET canonical_path = ? WHERE id = ?",
                        (canonical_path, relocated["source_id"]),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO inbox_entries(
                            canonical_path, sha256, size_bytes, mtime_ns, status,
                            discovered_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'metadata_required', ?, ?)
                        """,
                        (canonical_path, sha256, size_bytes, mtime_ns, now, now),
                    )
            elif existing["sha256"] != sha256:
                next_status = "ready" if existing["source_kind"] else "metadata_required"
                connection.execute(
                    """
                    UPDATE inbox_entries
                    SET sha256 = ?, size_bytes = ?, mtime_ns = ?, status = ?,
                        source_id = NULL, error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (sha256, size_bytes, mtime_ns, next_status, now, existing["id"]),
                )
            row = connection.execute(
                "SELECT * FROM inbox_entries WHERE canonical_path = ?", (canonical_path,)
            ).fetchone()
        return dict(row)

    def list_inbox_entries(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM inbox_entries ORDER BY discovered_at DESC, id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_inbox_entry(self, entry_id: int) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM inbox_entries WHERE id = ?", (entry_id,)
            ).fetchone()
        if row is None:
            raise RecordNotFound(f"Inbox entry {entry_id} does not exist")
        return dict(row)

    def set_inbox_metadata(
        self,
        entry_id: int,
        *,
        title: str,
        source_kind: str,
        author: str | None,
        authority: str,
        occurred_from: str | None,
        occurred_to: str | None,
        tags: list[str],
    ) -> dict[str, Any]:
        allowed_kinds = {"conversation", "user_note", "external_reference", "other"}
        allowed_authorities = {"primary", "user_analysis", "external", "unknown"}
        if source_kind not in allowed_kinds:
            raise ValueError(f"Unsupported source kind: {source_kind}")
        if authority not in allowed_authorities:
            raise ValueError(f"Unsupported source authority: {authority}")
        with self._connection(transactional=True) as connection:
            cursor = connection.execute(
                """
                UPDATE inbox_entries
                SET title = ?, source_kind = ?, author = ?, authority = ?,
                    occurred_from = ?, occurred_to = ?, tags_json = ?, status = 'ready',
                    error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    title.strip(),
                    source_kind,
                    author.strip() if author else None,
                    authority,
                    occurred_from or None,
                    occurred_to or None,
                    json.dumps(tags, ensure_ascii=False),
                    utc_now(),
                    entry_id,
                ),
            )
            if cursor.rowcount == 0:
                raise RecordNotFound(f"Inbox entry {entry_id} does not exist")
        return self.get_inbox_entry(entry_id)

    def mark_inbox_status(
        self, entry_id: int, status: str, *, source_id: int | None = None, error: str | None = None
    ) -> None:
        with self._connection(transactional=True) as connection:
            connection.execute(
                """
                UPDATE inbox_entries
                SET status = ?, source_id = COALESCE(?, source_id), error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, source_id, error, utc_now(), entry_id),
            )

    def recover_inbox_jobs(self) -> None:
        with self._connection(transactional=True) as connection:
            connection.execute(
                """
                UPDATE inbox_entries
                SET status = 'ready',
                    error = 'Recovered after interrupted processing', updated_at = ?
                WHERE status IN ('queued', 'processing')
                """,
                (utc_now(),),
            )

    def set_source_metadata(self, source_version_id: int, entry: dict[str, Any]) -> None:
        with self._connection(transactional=True) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO source_metadata(
                    source_version_id, title, source_kind, author, authority,
                    occurred_from, occurred_to, tags_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_version_id,
                    entry["title"],
                    entry["source_kind"],
                    entry["author"],
                    entry["authority"],
                    entry["occurred_from"],
                    entry["occurred_to"],
                    entry["tags_json"],
                ),
            )

    def get_source_metadata(self, source_version_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM source_metadata WHERE source_version_id = ?", (source_version_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_review_proposals(self, status: str = "pending") -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT rp.*, sc.statement AS subject_statement, oc.statement AS object_statement
                FROM review_proposals rp
                LEFT JOIN claims sc ON rp.subject_type = 'claim' AND sc.id = rp.subject_id
                LEFT JOIN claims oc ON rp.object_type = 'claim' AND oc.id = rp.object_id
                WHERE rp.status = ? ORDER BY rp.id
                """,
                (status,),
            ).fetchall()
        return [dict(row) for row in rows]

    def review_proposal(self, proposal_id: int, accept: bool) -> dict[str, Any]:
        now = utc_now()
        next_status = "accepted" if accept else "rejected"
        action = "accept_proposal" if accept else "reject_proposal"
        with self._connection(transactional=True) as connection:
            row = connection.execute(
                "SELECT * FROM review_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFound(f"Review proposal {proposal_id} does not exist")
            if row["status"] != "pending":
                raise ValueError(f"Review proposal {proposal_id} is already resolved")
            if (
                accept
                and row["subject_type"] == "claim"
                and row["object_type"] == "claim"
                and row["proposal_type"] in {"contradiction", "supersedes"}
            ):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO claim_links(
                        source_claim_id, relation_type, target_claim_id,
                        review_proposal_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        row["subject_id"],
                        "contradicts" if row["proposal_type"] == "contradiction" else "supersedes",
                        row["object_id"],
                        proposal_id,
                        now,
                    ),
                )
            connection.execute(
                "UPDATE review_proposals SET status = ?, resolved_at = ? WHERE id = ?",
                (next_status, now, proposal_id),
            )
            connection.execute(
                """
                INSERT INTO review_actions(proposal_id, action, created_at)
                VALUES (?, ?, ?)
                """,
                (proposal_id, action, now),
            )
        return {"id": proposal_id, "status": next_status}

    def review_claim(
        self,
        claim_id: int,
        action: str,
        *,
        statement: str | None = None,
        kind: str | None = None,
        lifecycle: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if action not in {"confirm", "reject", "override"}:
            raise ValueError(f"Unsupported review action: {action}")
        claim = self.get_claim(claim_id)
        now = utc_now()
        with self._connection(transactional=True) as connection:
            if action == "override":
                if not statement or not reason:
                    raise ValueError("Override requires a statement and reason")
                connection.execute(
                    "UPDATE claims SET review_status = 'overridden' WHERE id = ?", (claim_id,)
                )
                connection.execute(
                    """
                    INSERT INTO user_overrides(
                        target_claim_id, statement, kind, lifecycle, actor,
                        valid_from, valid_to, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claim_id,
                        statement.strip(),
                        kind or claim.get("kind", "observation"),
                        lifecycle or claim.get("lifecycle", "unknown"),
                        claim.get("actor"),
                        claim.get("valid_from"),
                        claim.get("valid_to"),
                        reason.strip(),
                        now,
                    ),
                )
            else:
                status = "confirmed" if action == "confirm" else "rejected"
                connection.execute(
                    "UPDATE claims SET review_status = ? WHERE id = ?", (status, claim_id)
                )
            connection.execute(
                """
                INSERT INTO review_actions(claim_id, action, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    claim_id,
                    action,
                    json.dumps(
                        {
                            "statement": statement,
                            "kind": kind,
                            "lifecycle": lifecycle,
                            "reason": reason,
                        },
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )
        return {"id": claim_id, "review_status": "overridden" if action == "override" else status}

    def get_dossier(self) -> dict[str, Any]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT c.id,
                       COALESCE(u.statement, c.statement) AS statement,
                       COALESCE(u.kind, c.kind) AS kind,
                       COALESCE(u.lifecycle, c.lifecycle) AS lifecycle,
                       COALESCE(u.actor, c.actor) AS actor,
                       COALESCE(u.valid_from, c.valid_from) AS valid_from,
                       COALESCE(u.valid_to, c.valid_to) AS valid_to,
                       c.confidence, c.review_status,
                       u.reason AS override_reason, u.created_at AS override_created_at,
                       EXISTS(
                           SELECT 1 FROM claim_links cl
                           WHERE cl.target_claim_id = c.id AND cl.relation_type = 'supersedes'
                       ) AS superseded,
                       ce.quote, ce.char_start, ce.char_end,
                       s.id AS source_id, s.canonical_path, sv.revision_no AS revision,
                       sm.title AS source_title, sm.author AS source_author,
                       sm.authority AS source_authority, sm.occurred_from
                FROM claims c
                JOIN jobs j ON j.id = c.job_id AND j.status = 'succeeded'
                JOIN source_versions sv ON sv.id = c.source_version_id
                JOIN sources s ON s.current_version_id = sv.id AND sv.active_job_id = j.id
                JOIN claim_evidence ce ON ce.claim_id = c.id
                LEFT JOIN source_metadata sm ON sm.source_version_id = sv.id
                LEFT JOIN user_overrides u ON u.id = (
                    SELECT MAX(u2.id) FROM user_overrides u2 WHERE u2.target_claim_id = c.id
                )
                WHERE c.review_status != 'rejected'
                ORDER BY COALESCE(
                    u.valid_from, c.valid_from, sm.occurred_from, sv.observed_at
                ), c.id
                """
            ).fetchall()
            summaries = connection.execute(
                """
                SELECT ss.*, s.id AS source_id, s.canonical_path
                FROM source_summaries ss
                JOIN source_versions sv ON sv.id = ss.source_version_id
                JOIN sources s ON s.current_version_id = sv.id
                WHERE sv.active_job_id = ss.job_id
                ORDER BY ss.source_version_id
                """
            ).fetchall()
            relations = connection.execute(
                """
                SELECT r.id, se.canonical_name AS source, r.predicate,
                       te.canonical_name AS target, r.confidence, r.status,
                       r.evidence_claim_id
                FROM relations r
                JOIN entities se ON se.id = r.source_entity_id
                JOIN entities te ON te.id = r.target_entity_id
                JOIN claims c ON c.id = r.evidence_claim_id
                JOIN source_versions sv ON sv.id = c.source_version_id
                WHERE r.status != 'rejected' AND c.review_status != 'rejected'
                  AND sv.active_job_id = c.job_id
                ORDER BY r.id
                """
            ).fetchall()
            claim_links = connection.execute(
                """
                SELECT cl.id, cl.source_claim_id, cl.relation_type, cl.target_claim_id,
                       sc.statement AS source_statement, tc.statement AS target_statement,
                       cl.created_at
                FROM claim_links cl
                JOIN claims sc ON sc.id = cl.source_claim_id
                JOIN claims tc ON tc.id = cl.target_claim_id
                ORDER BY cl.id
                """
            ).fetchall()
        sections = {
            "problems": [],
            "goals": [],
            "ideas": [],
            "proposed_decisions": [],
            "active_decisions": [],
            "completed_decisions": [],
            "rejected_decisions": [],
            "superseded_decisions": [],
            "other_decisions": [],
            "action_items": [],
            "observations": [],
        }
        for row in map(dict, rows):
            kind = row["kind"]
            lifecycle = row["lifecycle"]
            if kind == "decision":
                decision_section = (
                    "superseded_decisions"
                    if row["superseded"]
                    else {
                        "proposed": "proposed_decisions",
                        "active": "active_decisions",
                        "completed": "completed_decisions",
                        "rejected": "rejected_decisions",
                        "unknown": "other_decisions",
                    }[lifecycle]
                )
                sections[decision_section].append(row)
            else:
                sections[
                    {
                        "problem": "problems",
                        "goal": "goals",
                        "idea": "ideas",
                        "action_item": "action_items",
                        "observation": "observations",
                    }[kind]
                ].append(row)
        return {
            "sections": sections,
            "summaries": [dict(row) for row in summaries],
            "relations": [dict(row) for row in relations],
            "claim_links": [dict(row) for row in claim_links],
            "pending_reviews": len(self.list_review_proposals()),
        }

    def start_evaluation(self, source_version_id: int, prompt_version: str) -> int:
        with self._connection(transactional=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO evaluation_runs(source_version_id, prompt_version, status, started_at)
                VALUES (?, ?, 'running', ?)
                """,
                (source_version_id, prompt_version, utc_now()),
            )
        return int(cursor.lastrowid)

    def record_evaluation_result(self, evaluation_run_id: int, result: dict[str, Any]) -> None:
        with self._connection(transactional=True) as connection:
            connection.execute(
                """
                INSERT INTO evaluation_results(
                    evaluation_run_id, model, segment_id, schema_valid, evidence_valid,
                    claim_count, input_tokens, output_tokens, duration_ms, error, raw_output
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_run_id,
                    result["model"],
                    result["segment_id"],
                    int(result["schema_valid"]),
                    int(result["evidence_valid"]),
                    result["claim_count"],
                    result.get("input_tokens"),
                    result.get("output_tokens"),
                    result["duration_ms"],
                    result.get("error"),
                    result.get("raw_output"),
                ),
            )

    def finish_evaluation(self, evaluation_run_id: int, error: str | None = None) -> None:
        with self._connection(transactional=True) as connection:
            connection.execute(
                """
                UPDATE evaluation_runs
                SET status = ?, finished_at = ?, error = ? WHERE id = ?
                """,
                ("failed" if error else "succeeded", utc_now(), error, evaluation_run_id),
            )

    def list_evaluations(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            runs = connection.execute(
                """
                SELECT er.*, s.id AS source_id, s.canonical_path
                FROM evaluation_runs er
                JOIN source_versions sv ON sv.id = er.source_version_id
                JOIN sources s ON s.id = sv.source_id
                ORDER BY er.id DESC
                """
            ).fetchall()
            results = connection.execute(
                """
                SELECT evaluation_run_id, model,
                       COUNT(*) AS segment_count,
                       SUM(schema_valid) AS schema_passes,
                       SUM(evidence_valid) AS evidence_passes,
                       SUM(claim_count) AS claim_count,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(duration_ms) AS duration_ms
                FROM evaluation_results
                GROUP BY evaluation_run_id, model
                ORDER BY evaluation_run_id DESC, model
                """
            ).fetchall()
        by_run: dict[int, list[dict[str, Any]]] = {}
        for row in results:
            by_run.setdefault(row["evaluation_run_id"], []).append(dict(row))
        output = []
        for run in runs:
            item = dict(run)
            item["models"] = by_run.get(run["id"], [])
            output.append(item)
        return output

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
