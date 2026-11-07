-- ==============================================================================
-- VERIFICATION: Fix required_files to match actual challenge repos
-- ==============================================================================
-- Both existing challenge repos have their test file at the root
-- (test_pipeline.py / test_query.py), not inside a tests/ directory.
-- The original migrations declared "tests/" which rejected every submission.

UPDATE challenges
SET required_files = '["pipeline.py", "requirements.txt"]'::jsonb
WHERE slug = 'data-engineering-etl-pool-exhaustion';

UPDATE challenges
SET required_files = '["query.sql", "schema.sql", "requirements.txt"]'::jsonb
WHERE slug = 'sql-data-analysis';