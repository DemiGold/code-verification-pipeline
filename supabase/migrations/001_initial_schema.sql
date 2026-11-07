-- ==============================================================================
-- VERIFICATION: Initial Database Schema & Row-Level Security (RLS)
-- ==============================================================================
-- Run this file to completely build the Verification database from scratch.
-- ==============================================================================

-- 1. Create Custom Types (Enums)
-- These ensure the database rejects bad data (e.g., if someone tries to set a status to 'BANANA')
CREATE TYPE user_role AS ENUM ('CANDIDATE', 'VALIDATOR', 'ADMIN', 'EMPLOYER');

CREATE TYPE submission_status AS ENUM (
    'PENDING', 
    'REJECTED_AT_EDGE', 
    'QUEUED', 
    'RUNNING', 
    'PASSED', 
    'FAILED', 
    'ERROR', 
    'VERIFIED'
);

-- 2. Users Table
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    github_username TEXT UNIQUE NOT NULL,
    email TEXT,
    role user_role NOT NULL DEFAULT 'CANDIDATE',
    display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3. Challenges Table
CREATE TABLE challenges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    track TEXT NOT NULL,
    repo_template_url TEXT NOT NULL,
    required_files JSONB NOT NULL,
    required_packages JSONB NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 4. Submissions Table
CREATE TABLE submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    challenge_id UUID NOT NULL REFERENCES challenges(id) ON DELETE RESTRICT,
    repo_url TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    status submission_status NOT NULL DEFAULT 'PENDING',
    rejection_reason TEXT,
    verified_at TIMESTAMPTZ,
    verified_by UUID REFERENCES users(id) ON DELETE SET NULL,
    defence_notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 5. Reports Table
CREATE TABLE reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_id UUID NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    markdown_content TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 6. Test Results Table (For Phase 2 Telemetry)
CREATE TABLE test_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_id UUID NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    test_name TEXT NOT NULL,
    passed BOOLEAN NOT NULL,
    duration_ms INT,
    memory_mb INT,
    stdout TEXT,
    stderr TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 7. Add Indexes for Performance
CREATE INDEX idx_users_role ON users(role);
CREATE INDEX idx_users_github_username ON users(github_username);
CREATE INDEX idx_submissions_user ON submissions(user_id);
CREATE INDEX idx_submissions_challenge ON submissions(challenge_id);
CREATE INDEX idx_submissions_status ON submissions(status);
CREATE INDEX idx_submissions_created_at ON submissions(created_at DESC);
CREATE INDEX idx_reports_submission ON reports(submission_id);

-- ==============================================================================
-- ROW-LEVEL SECURITY (RLS) POLICIES
-- ==============================================================================
-- NOTE: The FastAPI backend uses the Service Role Key (which bypasses RLS).
-- These policies protect against direct database access from the future frontend.

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE challenges ENABLE ROW LEVEL SECURITY;
ALTER TABLE submissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE test_results ENABLE ROW LEVEL SECURITY;

-- USERS POLICIES
-- Users can read their own profile
CREATE POLICY "Users can read own profile" ON users FOR SELECT USING (auth.uid() = id);
-- Users can update their own profile
CREATE POLICY "Users can update own profile" ON users FOR UPDATE USING (auth.uid() = id);
-- Validators and Admins can read all profiles
CREATE POLICY "Validators can read all profiles" ON users FOR SELECT USING (
    EXISTS (SELECT 1 FROM users WHERE id = auth.uid() AND role IN ('VALIDATOR', 'ADMIN'))
);

-- CHALLENGES POLICIES
-- Anyone can read active challenges
CREATE POLICY "Anyone can read active challenges" ON challenges FOR SELECT USING (is_active = true);
-- Only Admins can modify challenges
CREATE POLICY "Admins can modify challenges" ON challenges FOR ALL USING (
    EXISTS (SELECT 1 FROM users WHERE id = auth.uid() AND role = 'ADMIN')
);

-- SUBMISSIONS POLICIES
-- Candidates can see their own submissions
CREATE POLICY "Candidates can read own submissions" ON submissions FOR SELECT USING (auth.uid() = user_id);
-- Candidates can insert their own submissions
CREATE POLICY "Candidates can insert own submissions" ON submissions FOR INSERT WITH CHECK (auth.uid() = user_id);
-- Validators/Admins can read all submissions
CREATE POLICY "Validators can read all submissions" ON submissions FOR SELECT USING (
    EXISTS (SELECT 1 FROM users WHERE id = auth.uid() AND role IN ('VALIDATOR', 'ADMIN'))
);
-- Validators/Admins can update submissions (e.g., to mark VERIFIED)
CREATE POLICY "Validators can update submissions" ON submissions FOR UPDATE USING (
    EXISTS (SELECT 1 FROM users WHERE id = auth.uid() AND role IN ('VALIDATOR', 'ADMIN'))
);

-- REPORTS POLICIES
-- Candidates can read reports for their own submissions
CREATE POLICY "Candidates can read own reports" ON reports FOR SELECT USING (
    EXISTS (SELECT 1 FROM submissions WHERE submissions.id = reports.submission_id AND submissions.user_id = auth.uid())
);
-- Validators/Admins can read all reports
CREATE POLICY "Validators can read all reports" ON reports FOR SELECT USING (
    EXISTS (SELECT 1 FROM users WHERE id = auth.uid() AND role IN ('VALIDATOR', 'ADMIN'))
);

-- TEST RESULTS POLICIES
-- Candidates can read test results for their own submissions
CREATE POLICY "Candidates can read own test results" ON test_results FOR SELECT USING (
    EXISTS (SELECT 1 FROM submissions WHERE submissions.id = test_results.submission_id AND submissions.user_id = auth.uid())
);
-- Validators/Admins can read all test results
CREATE POLICY "Validators can read all test results" ON test_results FOR SELECT USING (
    EXISTS (SELECT 1 FROM users WHERE id = auth.uid() AND role IN ('VALIDATOR', 'ADMIN'))
);

-- ==============================================================================
-- SEED DATA
-- ==============================================================================
-- Insert the v0.1 Data Engineering Challenge
INSERT INTO challenges (
    slug, title, description, track, repo_template_url, required_files, required_packages
) VALUES (
    'data-engineering-etl-pool-exhaustion',
    'Fix the Broken ETL Pipeline',
    'A Python ETL pipeline opens a new database connection for every record, causing connection-pool exhaustion under load.',
    'data-engineering',
    'https://github.com/verification-hq/challenge-etl-pool-exhaustion',
    '["pipeline.py", "requirements.txt", "tests/"]'::jsonb,
    '["sqlalchemy", "psycopg2", "pytest"]'::jsonb
);