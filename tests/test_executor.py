"""execute() itself never makes an LLM call, so these run on every `make test`."""

import time

from t2sql.execution import execute
from t2sql.execution.models import ResultSet


def test_execute_returns_rows_and_types() -> None:
    result = execute("SELECT id, name FROM customers ORDER BY id LIMIT 3")

    assert result.ok is True
    assert result.error is None
    assert result.row_count == 3
    assert result.result_set is not None
    assert result.result_set.columns == ["id", "name"]
    assert result.column_types == ["int8", "text"]
    assert result.result_set.rows[0][0] == 1


def test_execute_caps_rows_at_row_cap() -> None:
    result = execute("SELECT id FROM orders", row_cap=10)

    assert result.ok is True
    assert result.row_count == 10
    assert len(result.result_set.rows) == 10


def test_execute_pg_sleep_times_out_cleanly_instead_of_hanging() -> None:
    start = time.monotonic()
    result = execute("SELECT pg_sleep(10)", timeout=1)
    elapsed = time.monotonic() - start

    assert result.ok is False
    assert result.timed_out is True
    assert "timeout" in result.error.lower()
    assert elapsed < 5, f"took {elapsed:.1f}s -- should have been cancelled around 1s"


def test_execute_db_error_is_captured_not_raised() -> None:
    result = execute("SELECT 1/0")

    assert result.ok is False
    assert result.timed_out is False
    assert "division by zero" in result.error


def test_result_set_fingerprint_is_row_and_column_order_independent() -> None:
    a = ResultSet(columns=["id", "name"], rows=[[1, "a"], [2, "b"]])
    b = ResultSet(columns=["id", "name"], rows=[[2, "b"], [1, "a"]])
    c = ResultSet(columns=["name", "id"], rows=[["a", 1], ["b", 2]])
    d = ResultSet(columns=["id", "name"], rows=[[1, "a"], [2, "c"]])

    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() == c.fingerprint()
    assert a.fingerprint() != d.fingerprint()


def test_result_set_fingerprint_distinguishes_null_from_the_string_none() -> None:
    a = ResultSet(columns=["x"], rows=[[None]])
    b = ResultSet(columns=["x"], rows=[["None"]])

    assert a.fingerprint() != b.fingerprint()
