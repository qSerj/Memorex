BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS packets (
    id TEXT PRIMARY KEY,
    user_note TEXT NOT NULL DEFAULT '',
    note_source_revision_id INTEGER REFERENCES source_revisions(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS packet_items (
    id TEXT PRIMARY KEY,
    packet_id TEXT NOT NULL REFERENCES packets(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    kind TEXT NOT NULL CHECK (kind IN ('file', 'url')),
    display_name TEXT NOT NULL,
    url TEXT,
    mime_type TEXT,
    source_revision_id INTEGER REFERENCES source_revisions(id),
    status TEXT NOT NULL CHECK (status IN ('ready', 'waiting_importer', 'failed')),
    error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(packet_id, ordinal)
);

CREATE TABLE IF NOT EXISTS job_packets (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    packet_id TEXT NOT NULL REFERENCES packets(id)
);

CREATE INDEX IF NOT EXISTS packet_items_packet_idx ON packet_items(packet_id, ordinal);
CREATE INDEX IF NOT EXISTS job_packets_packet_idx ON job_packets(packet_id);

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (4, 'wiki_first_packets', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
