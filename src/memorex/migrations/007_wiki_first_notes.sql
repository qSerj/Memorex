BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS notebooks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    system_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    notebook_id TEXT NOT NULL REFERENCES notebooks(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS note_attachments (
    id TEXT PRIMARY KEY,
    note_id TEXT NOT NULL REFERENCES notes(id),
    source_revision_id INTEGER NOT NULL REFERENCES source_revisions(id),
    display_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at TEXT NOT NULL,
    removed_at TEXT,
    UNIQUE(note_id, ordinal)
);

CREATE TABLE IF NOT EXISTS discussion_notes (
    session_id TEXT NOT NULL REFERENCES chat_sessions(id),
    note_id TEXT NOT NULL REFERENCES notes(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY(session_id, note_id)
);

CREATE TABLE IF NOT EXISTS discussion_turns (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id),
    question_message_id INTEGER NOT NULL REFERENCES chat_messages(id),
    answer_message_id INTEGER REFERENCES chat_messages(id),
    task_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')
    ),
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discussion_turn_notes (
    turn_id TEXT NOT NULL REFERENCES discussion_turns(id),
    note_id TEXT NOT NULL REFERENCES notes(id),
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY(turn_id, note_id)
);

CREATE INDEX IF NOT EXISTS notes_notebook_idx ON notes(notebook_id, updated_at);
CREATE INDEX IF NOT EXISTS note_attachments_note_idx ON note_attachments(note_id, ordinal);
CREATE INDEX IF NOT EXISTS discussion_notes_session_idx ON discussion_notes(session_id, ordinal);
CREATE INDEX IF NOT EXISTS discussion_turns_session_idx ON discussion_turns(session_id, created_at);

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (7, 'wiki_first_notes', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
