import psycopg
import pytest

from t2sql.db.connection import get_connection


def test_readonly_role_cannot_write() -> None:
    with get_connection(role="readonly") as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("CREATE TABLE should_not_exist (id serial primary key)")
