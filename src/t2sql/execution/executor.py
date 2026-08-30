"""Runs SQL against the app_readonly connection with a hard wall-clock
timeout, and captures a bounded, structured result -- never raises on a
query error or timeout, always returns an ExecutionResult describing what
happened.
"""

from __future__ import annotations

import time

import psycopg

from t2sql.db.connection import get_connection
from t2sql.execution.models import ExecutionResult, ResultSet

DEFAULT_TIMEOUT_SECONDS = 5
DEFAULT_ROW_CAP = 1000


def _type_name(oid: int) -> str:
    type_info = psycopg.postgres.types.get(oid)
    return type_info.name if type_info else f"oid:{oid}"


def execute(
    sql: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    conn: psycopg.Connection | None = None,
    row_cap: int = DEFAULT_ROW_CAP,
) -> ExecutionResult:
    """Execute `sql` (readonly) and capture the result, or a clean error.

    `timeout` is enforced server-side via `statement_timeout` on this call's
    connection, so a hanging query (e.g. pg_sleep) is cancelled rather than
    left to hang -- this is on top of, not instead of, the app_readonly
    role's own default statement_timeout.

    `row_cap` bounds how many rows are pulled into the result payload. It
    does not itself limit server-side execution -- pair this with the AST
    validator, which injects `LIMIT` into the SQL before it gets here.
    """
    if conn is None:
        with get_connection(role="readonly") as owned_conn:
            return execute(sql, timeout=timeout, conn=owned_conn, row_cap=row_cap)

    start = time.monotonic()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(timeout * 1000)}")
            cur.execute(sql)
            if cur.description is None:
                return ExecutionResult(
                    ok=True,
                    sql=sql,
                    result_set=ResultSet(columns=[], rows=[]),
                    wall_time_seconds=time.monotonic() - start,
                )
            columns = [desc.name for desc in cur.description]
            column_types = [_type_name(desc.type_code) for desc in cur.description]
            rows = [list(row) for row in cur.fetchmany(row_cap)]
    except psycopg.errors.QueryCanceled:
        conn.rollback()
        return ExecutionResult(
            ok=False,
            sql=sql,
            error=f"query exceeded timeout of {timeout}s and was cancelled",
            timed_out=True,
            wall_time_seconds=time.monotonic() - start,
        )
    except psycopg.Error as e:
        conn.rollback()
        return ExecutionResult(
            ok=False,
            sql=sql,
            error=str(e).strip(),
            wall_time_seconds=time.monotonic() - start,
        )

    return ExecutionResult(
        ok=True,
        sql=sql,
        result_set=ResultSet(columns=columns, rows=rows),
        column_types=column_types,
        row_count=len(rows),
        wall_time_seconds=time.monotonic() - start,
    )
