-- ==============================================================================
-- VERIFICATION: Migration 005 — Add REJECTED to submission_status enum
-- ==============================================================================
-- The dashboard and API write status = 'REJECTED' when a reviewer rejects a
-- submission after the defence session. That value was never added to the
-- enum (only 'REJECTED_AT_EDGE' exists), so every reject click raised:
--
--   invalid input value for enum submission_status: "REJECTED"
--
-- This migration adds it. Existing rows are untouched.
--
-- NOTE: ALTER TYPE ... ADD VALUE cannot run inside a transaction in older
-- Postgres versions. Supabase runs each migration in its own transaction by
-- default; IF NOT EXISTS makes this idempotent and safe to re-run.
-- ==============================================================================

ALTER TYPE submission_status ADD VALUE IF NOT EXISTS 'REJECTED';