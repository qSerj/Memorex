BEGIN IMMEDIATE;

ALTER TABLE runner_calls ADD COLUMN profile TEXT;
ALTER TABLE runner_calls ADD COLUMN effort TEXT;

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (8, 'wiki_first_model_profiles', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
