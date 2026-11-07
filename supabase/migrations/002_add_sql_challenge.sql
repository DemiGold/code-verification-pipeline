-- ==============================================================================
-- VERIFICATION: Add SQL/Data Analysis Challenge
-- ==============================================================================

INSERT INTO challenges (
    slug, title, description, track, repo_template_url, required_files, required_packages
) VALUES (
    'sql-data-analysis',
    'Optimize the Slow Sales Report',
    'A critical sales report is taking 45 seconds to run in production. You have been given the schema and the slow query. Your task is to rewrite the SQL query and add the necessary indexes to get the execution time under 500ms.',
    'data-analysis',
    'https://github.com/verification-hq/challenge-sql-optimization',
    '["query.sql", "schema.sql", "tests/"]'::jsonb,
    '["pytest", "psycopg2-binary"]'::jsonb
);