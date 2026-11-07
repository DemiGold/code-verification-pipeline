import io
import zipfile

from app.services.edge_filter import validate_submission

# Simulate what the database says the challenges require
ETL_REQUIRED = ["pipeline.py", "requirements.txt"]
SQL_REQUIRED = ["query.sql", "schema.sql", "requirements.txt"]


def _make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


# --- ETL Tests ---

def test_rejects_missing_requirements():
    z = _make_zip({"pipeline.py": "x = 1\n"})
    ok, msg = validate_submission(z, ETL_REQUIRED)
    assert ok is False and "requirements.txt" in msg


def test_rejects_syntax_error():
    z = _make_zip({
        "requirements.txt": "pytest\n",
        "pipeline.py": "def broken(:\n    pass\n",
    })
    ok, msg = validate_submission(z, ETL_REQUIRED)
    assert ok is False and "Syntax error" in msg


def test_accepts_valid_etl_zip():
    z = _make_zip({
        "requirements.txt": "pytest\n",
        "pipeline.py": "x = 1\n",
    })
    ok, _ = validate_submission(z, ETL_REQUIRED)
    assert ok is True


# --- SQL Tests ---

def test_accepts_valid_sql_zip():
    """SQL challenge has no pipeline.py — proves the dynamic filter works."""
    z = _make_zip({
        "requirements.txt": "pytest\n",
        "query.sql": "SELECT 1;\n",
        "schema.sql": "CREATE TABLE t (id INT);\n",
    })
    ok, msg = validate_submission(z, SQL_REQUIRED)
    assert ok is True, f"SQL submission was rejected: {msg}"


def test_sql_missing_query_file_rejected():
    z = _make_zip({
        "requirements.txt": "pytest\n",
        "schema.sql": "CREATE TABLE t (id INT);\n",
    })
    ok, msg = validate_submission(z, SQL_REQUIRED)
    assert ok is False and "query.sql" in msg