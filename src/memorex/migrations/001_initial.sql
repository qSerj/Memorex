CREATE TABLE objects (
    sha256 TEXT PRIMARY KEY,
    relative_path TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    canonical_path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    current_version_id INTEGER
);

CREATE TABLE source_versions (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    revision_no INTEGER NOT NULL CHECK (revision_no > 0),
    object_sha256 TEXT NOT NULL REFERENCES objects(sha256) ON DELETE RESTRICT,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    observed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('accepted', 'failed')),
    parser_name TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    active_job_id INTEGER,
    UNIQUE (source_id, revision_no)
);

CREATE TABLE segments (
    id INTEGER PRIMARY KEY,
    source_version_id INTEGER NOT NULL REFERENCES source_versions(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    text TEXT NOT NULL,
    section TEXT,
    char_start INTEGER NOT NULL CHECK (char_start >= 0),
    char_end INTEGER NOT NULL CHECK (char_end > char_start),
    UNIQUE (source_version_id, ordinal)
);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('extract')),
    source_version_id INTEGER NOT NULL REFERENCES source_versions(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    base_url TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);

CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY,
    job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    purpose TEXT NOT NULL CHECK (purpose IN ('extract', 'answer')),
    segment_id INTEGER REFERENCES segments(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    model TEXT NOT NULL,
    base_url TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    validation_valid INTEGER NOT NULL CHECK (validation_valid IN (0, 1)),
    input_tokens INTEGER,
    output_tokens INTEGER,
    error TEXT,
    raw_output TEXT
);

CREATE TABLE claims (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source_version_id INTEGER NOT NULL REFERENCES source_versions(id) ON DELETE CASCADE,
    statement TEXT NOT NULL,
    normalized_statement TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL CHECK (status IN ('stated')),
    created_at TEXT NOT NULL
);

CREATE TABLE claim_evidence (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL UNIQUE REFERENCES claims(id) ON DELETE CASCADE,
    segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE RESTRICT,
    quote TEXT NOT NULL,
    char_start INTEGER NOT NULL CHECK (char_start >= 0),
    char_end INTEGER NOT NULL CHECK (char_end > char_start)
);

CREATE VIRTUAL TABLE claims_fts USING fts5(
    statement,
    content='claims',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER claims_ai AFTER INSERT ON claims BEGIN
    INSERT INTO claims_fts(rowid, statement) VALUES (new.id, new.statement);
END;

CREATE TRIGGER claims_ad AFTER DELETE ON claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, statement)
    VALUES ('delete', old.id, old.statement);
END;

CREATE TRIGGER claims_au AFTER UPDATE ON claims BEGIN
    INSERT INTO claims_fts(claims_fts, rowid, statement)
    VALUES ('delete', old.id, old.statement);
    INSERT INTO claims_fts(rowid, statement) VALUES (new.id, new.statement);
END;

CREATE INDEX source_versions_source_idx ON source_versions(source_id, revision_no);
CREATE INDEX segments_version_idx ON segments(source_version_id, ordinal);
CREATE INDEX jobs_version_idx ON jobs(source_version_id, status);
CREATE INDEX claims_job_idx ON claims(job_id);

