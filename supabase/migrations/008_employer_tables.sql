-- ==============================================================================
-- VERIFICATION: Migration 008 — Employer & Billing Schemas
-- ==============================================================================
-- Prepares the database for Phase 5 (Stripe + Employer Portal).
-- ==============================================================================

-- 1. Employers Table
CREATE TABLE employers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_name TEXT NOT NULL,
    contact_email TEXT UNIQUE NOT NULL,
    stripe_customer_id TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Unlocks Table (Tracks which employer unlocked which candidate)
CREATE TABLE unlocks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    employer_id UUID NOT NULL REFERENCES employers(id) ON DELETE CASCADE,
    submission_id UUID NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    unlocked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Prevent an employer from paying twice for the same candidate
    UNIQUE(employer_id, submission_id) 
);

-- 3. RLS Policies
ALTER TABLE employers ENABLE ROW LEVEL SECURITY;
ALTER TABLE unlocks ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Employers can read own profile" ON employers FOR SELECT USING (
    auth.uid()::text = contact_email
);

CREATE POLICY "Employers can read own unlocks" ON unlocks FOR SELECT USING (
    EXISTS (
        SELECT 1 FROM employers 
        WHERE employers.id = unlocks.employer_id 
        AND employers.contact_email = auth.uid()::text
    )
);