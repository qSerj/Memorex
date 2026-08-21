ALTER TABLE claims ADD COLUMN kind TEXT NOT NULL DEFAULT 'observation'
    CHECK (kind IN ('observation', 'problem', 'goal', 'idea', 'decision', 'action_item'));
ALTER TABLE claims ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'unknown'
    CHECK (lifecycle IN ('proposed', 'active', 'rejected', 'completed', 'unknown'));
ALTER TABLE claims ADD COLUMN polarity TEXT NOT NULL DEFAULT 'positive'
    CHECK (polarity IN ('positive', 'negative', 'unknown'));
ALTER TABLE claims ADD COLUMN actor TEXT;
ALTER TABLE claims ADD COLUMN valid_from TEXT;
ALTER TABLE claims ADD COLUMN valid_to TEXT;
ALTER TABLE claims ADD COLUMN review_status TEXT NOT NULL DEFAULT 'machine'
    CHECK (review_status IN ('machine', 'confirmed', 'rejected', 'overridden'));

CREATE TABLE inbox_entries (
    id INTEGER PRIMARY KEY,
    canonical_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    mtime_ns INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('metadata_required', 'ready', 'queued', 'processing', 'succeeded', 'failed')
    ),
    title TEXT,
    source_kind TEXT,
    author TEXT,
    authority TEXT,
    occurred_from TEXT,
    occurred_to TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    error TEXT,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE source_metadata (
    source_version_id INTEGER PRIMARY KEY REFERENCES source_versions(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (
        source_kind IN ('conversation', 'user_note', 'external_reference', 'other')
    ),
    author TEXT,
    authority TEXT NOT NULL CHECK (
        authority IN ('primary', 'user_analysis', 'external', 'unknown')
    ),
    occurred_from TEXT,
    occurred_to TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE entities (
    id INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK (
        entity_type IN ('person', 'organization', 'project', 'product', 'technology', 'concept', 'other')
    ),
    review_status TEXT NOT NULL DEFAULT 'machine'
        CHECK (review_status IN ('machine', 'confirmed', 'merged', 'rejected')),
    created_at TEXT NOT NULL,
    UNIQUE (normalized_name, entity_type)
);

CREATE TABLE entity_aliases (
    id INTEGER PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (entity_id, normalized_alias)
);

CREATE TABLE claim_entities (
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (role IN ('subject', 'actor', 'object', 'about')),
    PRIMARY KEY (claim_id, entity_id, role)
);

CREATE TABLE relations (
    id INTEGER PRIMARY KEY,
    source_entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE RESTRICT,
    predicate TEXT NOT NULL CHECK (
        predicate IN (
            'addresses', 'proposes', 'accepts', 'rejects', 'because_of',
            'depends_on', 'assigned_to', 'supports', 'contradicts', 'supersedes', 'about'
        )
    ),
    target_entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE RESTRICT,
    evidence_claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL DEFAULT 'machine'
        CHECK (status IN ('machine', 'confirmed', 'rejected')),
    created_at TEXT NOT NULL,
    UNIQUE (source_entity_id, predicate, target_entity_id, evidence_claim_id)
);

CREATE TABLE source_summaries (
    source_version_id INTEGER PRIMARY KEY REFERENCES source_versions(id) ON DELETE CASCADE,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE review_proposals (
    id INTEGER PRIMARY KEY,
    proposal_type TEXT NOT NULL CHECK (
        proposal_type IN ('entity_merge', 'contradiction', 'supersedes', 'relation')
    ),
    subject_type TEXT NOT NULL CHECK (subject_type IN ('claim', 'entity')),
    subject_id INTEGER NOT NULL,
    object_type TEXT NOT NULL CHECK (object_type IN ('claim', 'entity')),
    object_id INTEGER NOT NULL,
    rationale TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE (proposal_type, subject_type, subject_id, object_type, object_id)
);

CREATE TABLE review_actions (
    id INTEGER PRIMARY KEY,
    proposal_id INTEGER REFERENCES review_proposals(id) ON DELETE SET NULL,
    claim_id INTEGER REFERENCES claims(id) ON DELETE SET NULL,
    action TEXT NOT NULL CHECK (
        action IN ('confirm', 'reject', 'accept_proposal', 'reject_proposal', 'override')
    ),
    payload_json TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL
);

CREATE TABLE user_overrides (
    id INTEGER PRIMARY KEY,
    target_claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    statement TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN ('observation', 'problem', 'goal', 'idea', 'decision', 'action_item')
    ),
    lifecycle TEXT NOT NULL CHECK (
        lifecycle IN ('proposed', 'active', 'rejected', 'completed', 'unknown')
    ),
    actor TEXT,
    valid_from TEXT,
    valid_to TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE evaluation_runs (
    id INTEGER PRIMARY KEY,
    source_version_id INTEGER NOT NULL REFERENCES source_versions(id) ON DELETE CASCADE,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);

CREATE TABLE evaluation_results (
    id INTEGER PRIMARY KEY,
    evaluation_run_id INTEGER NOT NULL REFERENCES evaluation_runs(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    segment_id INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    schema_valid INTEGER NOT NULL CHECK (schema_valid IN (0, 1)),
    evidence_valid INTEGER NOT NULL CHECK (evidence_valid IN (0, 1)),
    claim_count INTEGER NOT NULL DEFAULT 0 CHECK (claim_count >= 0),
    input_tokens INTEGER,
    output_tokens INTEGER,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    error TEXT,
    raw_output TEXT,
    UNIQUE (evaluation_run_id, model, segment_id)
);

CREATE INDEX inbox_entries_status_idx ON inbox_entries(status);
CREATE INDEX claims_kind_lifecycle_idx ON claims(kind, lifecycle, review_status);
CREATE INDEX claim_entities_entity_idx ON claim_entities(entity_id);
CREATE INDEX relations_source_idx ON relations(source_entity_id, predicate);
CREATE INDEX relations_target_idx ON relations(target_entity_id, predicate);
CREATE INDEX review_proposals_status_idx ON review_proposals(status);
CREATE INDEX evaluation_results_run_idx ON evaluation_results(evaluation_run_id, model);
