-- ==============================================================================
-- VERIFICATION: Migration 006 — Passport Enrichment
-- ==============================================================================
-- Adds fields needed for the employer-facing Talent Passport.
-- ==============================================================================

-- 1. Add known_limitations to submissions
ALTER TABLE submissions ADD COLUMN known_limitations TEXT;

-- 2. Add public_slug to users (for clean URLs)
ALTER TABLE users ADD COLUMN public_slug TEXT UNIQUE;