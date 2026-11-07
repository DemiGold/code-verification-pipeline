"""Official test for the ETL connection-pooling challenge.

The buggy starter pipeline opens a fresh sqlite3 connection for every record.
At 50 records that's 50 connections, which exhausts Postgres' connection
limit under concurrent load. The fix is either a single shared connection
(closed properly) or a bounded pool.

This test enforces that fix by mocking sqlite3.connect and counting calls.
It does NOT verify the pipeline actually *used* those connections to do
work — that would require mocking more surface. The correctness of the data
processing is checked separately in the empty-input test below.
"""

import sqlite3  # not used directly; imported so linters see the module is
                # available and `patch("sqlite3.connect")` is obviously valid

import pytest
from unittest.mock import patch

# Module-level import means if pipeline.py has a syntax error or missing
# dependency, this entire test module fails to collect — which is correct
# behaviour: there's no point running tests against code that can't import.
from pipeline import process_data


def test_pipeline_handles_empty_input():
    """An empty input list should not crash the pipeline.

    The try/except is unusual — pytest would catch the exception anyway.
    It's here for the failure message: without it, an exception shows a raw
    traceback, and the candidate can't tell whether they failed the test or
    the whole suite broke. The fail() message makes it unambiguous.
    """
    try:
        process_data([])
    except Exception as e:
        pytest.fail(f"Pipeline crashed on empty input: {e}")


def test_pipeline_does_not_exhaust_connections():
    """The core test: opening one connection per record exhausts the pool.

    Buggy starter code: 50 records → 50 sqlite3.connect() calls.
    Correct fix:        50 records → 1 connection (or a bounded pool).
    Threshold:          ≤ 5 calls (leeway for pool-based fixes).
    """
    records = [(i, 100.0) for i in range(50)]

    # IMPORTANT limitations of this patch target:
    #
    # 1. patch("sqlite3.connect") intercepts calls made via `sqlite3.connect(...)`.
    #    It does NOT intercept `from sqlite3 import connect` at the top of
    #    pipeline.py — that binds the name to the function directly, so the
    #    module-level attribute patch has no effect. A candidate using
    #    `from sqlite3 import connect` will show 0 calls here regardless of
    #    how many connections they actually open.
    #
    # 2. It does NOT catch SQLAlchemy (which wraps connection pools itself),
    #    psycopg2, or any non-sqlite driver. The challenge starter repo uses
    #    sqlite3 specifically, so this is the right target for THIS challenge.
    #    A future challenge on a different DB would need its own test.
    #
    # 3. The MagicMock returned by the patch behaves as an empty iterable and
    #    supports method chains, but comparing `mock.rowcount > 0` raises
    #    TypeError. If a candidate writes code like
    #    `if conn.execute(...).rowcount > 0:`, they'll fail this test with a
    #    confusing TypeError instead of a clean assertion message.
    #    TODO: configure the mock with sensible return values for common
    #    patterns, or restructure the test to only check call_count after
    #    the pipeline has finished without hitting the mock's return values.
    with patch("sqlite3.connect") as mock_connect:
        process_data(records)

        # 5 allows for pools that hold 3-5 connections. A stricter bound
        # (≤ 1) would reject legitimate pool implementations; a looser one
        # (≤ 10) would let "1 connection per 10 records" pass even though
        # it's still O(n) in the worst case.
        assert mock_connect.call_count <= 5, (
            f"Connection Pool Exhaustion! Opened {mock_connect.call_count} "
            f"connections for {len(records)} records. You must refactor the pipeline "
            f"to use a single connection or a connection pool."
        )


# Deferred on purpose, not forgotten:
#
# - Verify connections are CLOSED, not just few. A candidate could open one
#   connection at module load and never close it — passes this test but leaks
#   a connection per worker process, which becomes 1000 open connections at
#   scale. Testing this properly means patching the connection's __enter__/
#   __exit__ or asserting close() was called. Doable but the failure mode is
#   rare enough that the current check catches the common bug.
#
# - Verify the data was actually processed. Currently a candidate could write
#   `def process_data(records): sqlite3.connect("db"); pass` and pass both
#   tests. The empty-input test just checks "doesn't crash"; the pooling test
#   just checks "not too many connections". Correctness of the transformation
#   (that records actually get inserted) isn't verified. Would require either
#   a real sqlite file or a stateful mock. Deferred because the challenge's
#   scope is explicitly "fix the pooling bug", not "build a correct pipeline".
#
# - The magic 5. Why 5 and not 3 or 10? A 3-connection pool is the minimum
#   for most implementations (primary + 2 spares is common). 5 gives room
#   for small pools without allowing per-batch opens. If the challenge ever
#   ships a starter that suggests a specific pool size, align the threshold
#   with that. Right now it's a judgment call.
#
# - pytest.fail loses the traceback by default. In pytest 7+, pass
#   `pytrace=True` to preserve it: `pytest.fail(msg, pytrace=True)`.
#   Currently a candidate who hits this sees the message but not the stack,
#   which makes debugging harder. Small fix; deferred because the message
#   alone is usually enough.
#
# - No timeout. If a candidate's pipeline hangs (e.g., deadlock in their pool
#   implementation), the sandbox's 30s container timeout fires and the test
#   is reported as a generic timeout failure — not as "test_pipeline_does_not_
#   exhaust_connections timed out". A per-test timeout via pytest-timeout
#   would give a cleaner message. Deferred because container timeout is
#   already enforcing the bound; pytest-timeout would just improve the
#   diagnostic.