"""Official test for the SQL index-optimisation challenge.

The challenge ships a slow sales report query plus a schema. The candidate's
fix has two parts:

    1. Rewrite query.sql so the SELECT can use an index
    2. Add a CREATE INDEX statement on sales(sale_date)

This test verifies the *structural* part — that the index was added and the
original SELECT survives. It does NOT:

    * Run the query against a real database
    * Measure latency (no 45s → 500ms check happens here)
    * Verify the index is actually used by the query planner
    * Check that the SELECT was rewritten to exploit the index

Those require running Postgres in the sandbox with seeded data and
EXPLAIN ANALYZE comparison — a much larger build. The structural test here
is the cheap first filter: it catches candidates who submitted without
adding the index at all. The behavioural verification happens on the review
call, not in this file.
"""

import re


def test_query_has_index():
    with open("query.sql", "r") as f:
        content = f.read()

    # --- Strip line comments ---
    # Only strips `--` at the START of a line. This is intentionally simple:
    # the starter query has no inline comments, so a candidate adding one
    # is rare. The gaps are real though:
    #
    #   * Inline comments survive. "SELECT * FROM sales -- CREATE INDEX
    #     idx ON sales(sale_date)" would pass, because the line starts with
    #     SELECT (not "--") so the stripping loop leaves it alone, and the
    #     index text inside the inline comment still matches the regex.
    #
    #   * Block comments survive. A "/* CREATE INDEX ... */" would pass for
    #     the same reason — nothing strips it.
    #
    # Neither path is exploitable in practice unless the candidate knows
    # the test's internals. Worth knowing they exist. See TODO for the
    # proper fix.
    lines = [line for line in content.split("\n") if not line.strip().startswith("--")]
    clean_sql = "\n".join(lines).upper()

    # --- Preserve the original query ---
    # Without this check a candidate could submit only "CREATE INDEX ...;"
    # and delete the SELECT entirely — technically adding the index, but
    # not solving the challenge. This is the minimum viable guard.
    #
    # It's loose: "SELECT" and "SALES" appearing anywhere in the file is
    # enough. "SELECT 1 FROM sales;" would pass despite not resembling the
    # original query. Verifying the exact structure was preserved would
    # need a real SQL parser or a hash of the original — more complexity
    # than this challenge justifies.
    assert "SELECT" in clean_sql and "SALES" in clean_sql, (
        "The original SELECT query must remain in the file."
    )

    # --- Look for the index ---
    #
    # Pattern breakdown:
    #
    #   CREATE\s+                — literal "CREATE" then whitespace. \s
    #                              matches spaces, tabs, and newlines, so
    #                              multi-line formatting works.
    #   (?:UNIQUE\s+)?           — optional "UNIQUE " prefix. A unique index
    #                              is a valid solution for this challenge.
    #   INDEX\s+                 — literal "INDEX" then whitespace
    #   (?:\S+\s+)?              — OPTIONAL single-token index name followed
    #                              by whitespace. Absent for Postgres's
    #                              unnamed-index syntax; present for the
    #                              common "CREATE INDEX idx_name ON ..." form.
    #   ON\s+                    — "ON" then whitespace
    #   \bSALES\b                — "SALES" as a whole word. \b on both sides
    #                              prevents "SUPER_SALES" and "SALES_STATUS"
    #                              from matching. (Python's \b treats "_" as
    #                              a word char, so \bSALES\b correctly skips
    #                              "_SALES" too.)
    #   \s*\(\s*                 — optional whitespace, open paren, optional
    #                              whitespace inside. Handles "sales(sale_date)",
    #                              "sales (sale_date)", "sales ( sale_date)".
    #   \bSALE_DATE\b            — "SALE_DATE" as a whole word inside the
    #                              parens. \b again prevents "MY_SALE_DATE"
    #                              and "sale_date_pk" from matching.
    #
    # Earlier version of this pattern used `.*?\s+ON\s+SALES` without the
    # optional-name group. That rejected Postgres's "CREATE INDEX ON
    # sales(sale_date)" (unnamed) form — the whitespace-required-before-ON
    # made the unnamed case impossible to match. Fixed here so both forms
    # are accepted.
    #
    # Still not matched:
    #
    #   * "USING btree" form. "CREATE INDEX idx ON sales USING btree
    #     (sale_date)" is valid Postgres but not matched because \s*\(
    #     expects "(" right after "SALES". Rare in candidate submissions —
    #     btree is the default so most people omit it. See TODO.
    #
    #   * Closing paren isn't verified. The match ends at SALE_DATE and
    #     doesn't check what follows. A composite index "(sale_date, other)"
    #     passes (arguably correct — a leading-column composite is usable);
    #     an unclosed "(sale_date_oops" also passes (not correct, but the
    #     sandbox doesn't execute SQL so this goes undetected).
    pattern = r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:\S+\s+)?ON\s+\bSALES\b\s*\(\s*\bSALE_DATE\b"

    match = re.search(pattern, clean_sql)

    assert match is not None, (
        "You must add a CREATE INDEX statement specifically targeting the `sale_date` column "
        "on the `sales` table. e.g., `CREATE INDEX idx_sales_date ON sales(sale_date);`"
    )


# Deferred on purpose, not forgotten:
#
# - "USING btree" form. "CREATE INDEX idx ON sales USING btree (sale_date)"
#   is valid Postgres (btree is the default method, so most candidates
#   omit it, but some include it explicitly). Not matched by the current
#   pattern. Fix: `\s+(?:USING\s+\w+\s+)?\(` between SALES and the paren.
#   Deferred because it's rare and adding it makes the regex harder to
#   read for a benefit that hasn't materialised in submissions.
#
# - Comment stripping is incomplete (see inline note). A candidate aware
#   of the test could hide the CREATE INDEX inside a /* */ block comment
#   and pass with zero real work. Fix: use `sqlparse` (a proper SQL
#   tokeniser) to strip comments statefully, or add explicit /* */
#   handling. Adding sqlparse means one more package in the sandbox
#   Dockerfile pip line.
#
# - No behavioural verification. This test is purely structural. A
#   candidate who adds the index but doesn't rewrite the SELECT passes
#   this test, yet their query remains slow. That's a real hole in the
#   challenge. Proper fix: seed a Postgres instance in the sandbox, run
#   EXPLAIN ANALYZE before and after, compare timings. The reviewer's live
#   defence is meant to catch this — but relying on human review for a
#   check that could be automated is a known gap.
#
# - No check that the index is actually useful. A candidate could add
#   "CREATE INDEX idx ON sales(sale_date + interval '0 day')" — an
#   expression index that the planner can't use for a WHERE sale_date
#   BETWEEN query. This test would pass; the query would stay slow. Only
#   an EXPLAIN ANALYZE check catches it.
#
# - The failure message example hardcodes "idx_sales_date". This encourages
#   candidates to use that exact name — the test doesn't care, but the
#   message implies it might. Not a bug, just a UX wart. If a candidate
#   complains they used "idx_date" and got the message, that's the
#   message's fault.
#
# - No IF NOT EXISTS / CONCURRENTLY check. Production DDL should use
#   "CREATE INDEX CONCURRENTLY" to avoid locking the table during
#   creation. Given the challenge is a learning exercise, checking for
#   CONCURRENTLY would teach good practice — but the reviewer can raise
#   this in the defence call. Not the test's job to enforce.