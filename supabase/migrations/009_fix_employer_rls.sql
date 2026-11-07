-- ==============================================================================
-- VERIFICATION: Migration 009 — Fix Employer RLS Policies
-- ==============================================================================
-- Migration 008 compared auth.uid() (a UUID) to contact_email (a string).
-- That comparison is always false, so employers would see zero rows.
--
-- The correct way to match an authenticated Supabase user to a row by email
-- is `auth.jwt() ->> 'email'`, which returns the email claim from the JWT.
--
-- This migration drops the broken policies and recreates them correctly.
-- Idempotent: safe to re-run.
-- ==============================================================================

DROP POLICY IF EXISTS "Employers can read own profile" ON employers;
DROP POLICY IF EXISTS "Employers can read own unlocks" ON unlocks;

CREATE POLICY "Employers can read own profile" ON employers
    FOR SELECT
    USING (auth.jwt() ->> 'email' = contact_email);

CREATE POLICY "Employers can read own unlocks" ON unlocks
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1 FROM employers
            WHERE employers.id = unlocks.employer_id
              AND employers.contact_email = (auth.jwt() ->> 'email')
        )
    );