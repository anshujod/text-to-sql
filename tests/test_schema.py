from t2sql.db.connection import get_connection

EXPECTED_COLUMN_COUNTS = {
    "categories": 3,
    "customers": 3,
    "users": 6,
    "addresses": 10,
    "products": 6,
    "orders": 6,
    "order_items": 5,
    "payments": 6,
    "refunds": 7,
    "sessions": 4,
}


def test_all_tables_exist_with_expected_columns() -> None:
    with get_connection(role="readonly") as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, count(*)
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = ANY(%s)
                GROUP BY table_name
                """,
                (list(EXPECTED_COLUMN_COUNTS),),
            )
            actual = dict(cur.fetchall())

    assert set(actual) == set(EXPECTED_COLUMN_COUNTS)
    for table, expected_count in EXPECTED_COLUMN_COUNTS.items():
        assert actual[table] == expected_count, (
            f"{table}: expected {expected_count} columns, got {actual[table]}"
        )
