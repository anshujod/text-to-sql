""" ~25 adversarial SQL strings must be caught with the
correct error type, and ~15 valid queries must pass through untouched
(modulo the LIMIT/qualification rewrite)."""

import pytest

from t2sql.db.connection import get_connection
from t2sql.validation import ValidationErrorType, validate_sql

ADVERSARIAL_CASES: list[tuple[str, ValidationErrorType]] = [
    # Not a SELECT at all.
    ("DROP TABLE orders", ValidationErrorType.NOT_A_SELECT),
    ("DELETE FROM orders", ValidationErrorType.NOT_A_SELECT),
    ("UPDATE orders SET status = 'cancelled'", ValidationErrorType.NOT_A_SELECT),
    ("INSERT INTO orders (id) VALUES (1)", ValidationErrorType.NOT_A_SELECT),
    ("TRUNCATE orders", ValidationErrorType.NOT_A_SELECT),
    ("GRANT ALL ON orders TO PUBLIC", ValidationErrorType.NOT_A_SELECT),
    ("ALTER TABLE orders ADD COLUMN evil int", ValidationErrorType.NOT_A_SELECT),
    ("SET statement_timeout = 0", ValidationErrorType.NOT_A_SELECT),
    ("COPY orders TO STDOUT", ValidationErrorType.NOT_A_SELECT),
    ("CREATE TABLE evil (id int)", ValidationErrorType.NOT_A_SELECT),
    (
        "MERGE INTO orders USING customers ON orders.customer_id = customers.id "
        "WHEN MATCHED THEN DELETE",
        ValidationErrorType.NOT_A_SELECT,
    ),
    # Multi-statement / stacked-query injection.
    ("SELECT 1; DROP TABLE orders;", ValidationErrorType.MULTIPLE_STATEMENTS),
    ("SELECT * FROM orders; DELETE FROM orders;", ValidationErrorType.MULTIPLE_STATEMENTS),
    ("SELECT 1; SELECT 2;", ValidationErrorType.MULTIPLE_STATEMENTS),
    ("", ValidationErrorType.MULTIPLE_STATEMENTS),
    # CTE-wrapped write -- still a Select at the root, so this only gets
    # caught by walking the whole tree for forbidden node types.
    (
        "WITH x AS (DELETE FROM orders RETURNING id) SELECT * FROM x",
        ValidationErrorType.FORBIDDEN_STATEMENT,
    ),
    (
        "WITH x AS (UPDATE orders SET status = 'cancelled' RETURNING id) SELECT * FROM x",
        ValidationErrorType.FORBIDDEN_STATEMENT,
    ),
    # Unparseable.
    ("SELEC * FRM orders WHERE", ValidationErrorType.PARSE_ERROR),
    # Unknown tables/columns.
    ("SELECT * FROM nonexistent_table", ValidationErrorType.UNKNOWN_TABLE),
    (
        "SELECT * FROM orders o JOIN nonexistent_table n ON o.id = n.id",
        ValidationErrorType.UNKNOWN_TABLE,
    ),
    ("SELECT hallucinated_column FROM orders", ValidationErrorType.UNKNOWN_COLUMN),
    ("SELECT orders.hallucinated_column FROM orders", ValidationErrorType.UNKNOWN_COLUMN),
    # Cartesian products: comma-join, ON-less JOIN, and explicit CROSS JOIN.
    ("SELECT * FROM orders, customers", ValidationErrorType.CARTESIAN_PRODUCT),
    ("SELECT * FROM orders JOIN customers", ValidationErrorType.CARTESIAN_PRODUCT),
    ("SELECT * FROM orders CROSS JOIN customers", ValidationErrorType.CARTESIAN_PRODUCT),
    # Catalog access.
    ("SELECT * FROM pg_catalog.pg_tables", ValidationErrorType.CATALOG_ACCESS),
    ("SELECT * FROM information_schema.columns", ValidationErrorType.CATALOG_ACCESS),
    ("SELECT * FROM pg_stat_activity", ValidationErrorType.CATALOG_ACCESS),
    ("SELECT relname FROM pg_class", ValidationErrorType.CATALOG_ACCESS),
]

VALID_QUERIES: list[str] = [
    "SELECT COUNT(*) FROM orders",
    "SELECT id, status FROM orders",
    "SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id",
    "SELECT id, name FROM customers WHERE created_at > now() - interval '30 days'",
    "SELECT status, COUNT(*) FROM orders GROUP BY status",
    "SELECT * FROM customers",
    "SELECT * FROM customers LIMIT 5",
    "SELECT SUM(amount) FROM payments WHERE status = 'succeeded'",
    (
        "WITH recent AS (SELECT * FROM orders WHERE created_at > now() - interval '7 days') "
        "SELECT COUNT(*) FROM recent"
    ),
    "SELECT p.name, oi.quantity FROM order_items oi JOIN products p ON oi.product_id = p.id",
    "SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id ORDER BY COUNT(*) DESC LIMIT 10",
    "SELECT * FROM orders WHERE status IN ('paid', 'shipped')",
    "SELECT id FROM products WHERE deleted_at IS NULL",
    "SELECT AVG(amount) FROM payments",
    "SELECT DISTINCT currency FROM payments",
]


@pytest.mark.parametrize("sql,expected_type", ADVERSARIAL_CASES)
def test_adversarial_sql_is_rejected_with_correct_error_type(
    sql: str, expected_type: ValidationErrorType
) -> None:
    with get_connection(role="readonly") as conn:
        result = validate_sql(sql, conn=conn)

    assert result.ok is False
    assert result.rewritten_sql is None
    error_types = {e.type for e in result.errors}
    assert expected_type in error_types, f"expected {expected_type} in {error_types} for {sql!r}"


@pytest.mark.parametrize("sql", VALID_QUERIES)
def test_valid_sql_passes(sql: str) -> None:
    with get_connection(role="readonly") as conn:
        result = validate_sql(sql, conn=conn)

    assert result.ok is True, f"unexpected errors for {sql!r}: {result.errors}"
    assert result.errors == []
    assert result.rewritten_sql
    assert "LIMIT" in result.rewritten_sql.upper()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM orders",
        "SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id",
        "SELECT status, COUNT(*) FROM orders GROUP BY status",
        "SELECT * FROM orders WHERE status IN ('paid', 'shipped')",
        (
            "WITH recent AS (SELECT * FROM orders WHERE created_at > now() - interval '7 days') "
            "SELECT COUNT(*) FROM recent"
        ),
    ],
)
def test_rewritten_sql_actually_executes(sql: str) -> None:
    with get_connection(role="readonly") as conn:
        result = validate_sql(sql, conn=conn)
        assert result.ok
        with conn.cursor() as cur:
            cur.execute(result.rewritten_sql)
            cur.fetchall()  # must not raise


def test_limit_is_preserved_when_already_present() -> None:
    with get_connection(role="readonly") as conn:
        result = validate_sql("SELECT * FROM customers LIMIT 5", conn=conn)

    assert result.ok
    assert result.rewritten_sql.strip().upper().endswith("LIMIT 5")


# SELECT * width check exercised against a synthetic schema, since none of
# our real tables exceed the 20-column threshold.
_WIDE_SCHEMA = {
    "orders": {f"col{i}": "text" for i in range(25)},
    "customers": {"id": "bigint", "name": "text"},
}


def test_select_star_on_wide_table_is_rejected() -> None:
    result = validate_sql("SELECT * FROM orders", live_schema=_WIDE_SCHEMA)

    assert result.ok is False
    assert ValidationErrorType.WIDE_SELECT_STAR in {e.type for e in result.errors}


def test_select_star_with_table_alias_on_wide_table_is_rejected() -> None:
    result = validate_sql("SELECT o.* FROM orders o", live_schema=_WIDE_SCHEMA)

    assert result.ok is False
    assert ValidationErrorType.WIDE_SELECT_STAR in {e.type for e in result.errors}


def test_select_star_on_narrow_table_is_allowed_even_with_wide_table_in_schema() -> None:
    result = validate_sql("SELECT * FROM customers", live_schema=_WIDE_SCHEMA)

    assert result.ok is True
