CREATE TABLE claim_links (
    id INTEGER PRIMARY KEY,
    source_claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK (relation_type IN ('contradicts', 'supersedes')),
    target_claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    review_proposal_id INTEGER REFERENCES review_proposals(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_claim_id, relation_type, target_claim_id)
);

CREATE VIRTUAL TABLE user_overrides_fts USING fts5(
    statement,
    content='user_overrides',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER user_overrides_ai AFTER INSERT ON user_overrides BEGIN
    INSERT INTO user_overrides_fts(rowid, statement) VALUES (new.id, new.statement);
END;

CREATE TRIGGER user_overrides_ad AFTER DELETE ON user_overrides BEGIN
    INSERT INTO user_overrides_fts(user_overrides_fts, rowid, statement)
    VALUES ('delete', old.id, old.statement);
END;

CREATE TRIGGER user_overrides_au AFTER UPDATE ON user_overrides BEGIN
    INSERT INTO user_overrides_fts(user_overrides_fts, rowid, statement)
    VALUES ('delete', old.id, old.statement);
    INSERT INTO user_overrides_fts(rowid, statement) VALUES (new.id, new.statement);
END;

INSERT INTO user_overrides_fts(user_overrides_fts) VALUES ('rebuild');

CREATE INDEX claim_links_source_idx ON claim_links(source_claim_id, relation_type);
CREATE INDEX claim_links_target_idx ON claim_links(target_claim_id, relation_type);
