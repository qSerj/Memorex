BEGIN IMMEDIATE;

ALTER TABLE packet_items
ADD COLUMN processing_mode TEXT NOT NULL DEFAULT 'analyze'
CHECK (processing_mode IN ('analyze', 'store'));

ALTER TABLE packet_items
ADD COLUMN analysis_instruction TEXT NOT NULL DEFAULT '';

INSERT INTO schema_migrations(version, name, applied_at)
VALUES (6, 'wiki_first_packet_analysis', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
