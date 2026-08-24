BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS packet_queue (
    packet_id TEXT PRIMARY KEY REFERENCES packets(id),
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'retry_wait', 'review', 'done', 'failed', 'idle')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at TEXT NOT NULL,
    last_job_id TEXT REFERENCES jobs(id),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS packet_queue_due_idx
ON packet_queue(status, available_at, created_at);

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (5, 'wiki_first_packet_queue', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
