"""Static SQL validation via sqlglot -- runs before anything touches the
database (or before a repair loop retries).

Rejects, with a specific error type per problem:
  - anything that isn't a single SELECT (multi-statement, DML/DDL, a
    CTE-wrapped write, or an unparseable string)
  - unknown tables/columns, checked against the live schema
  - `SELECT *` on a table with more than WIDE_TABLE_COLUMN_THRESHOLD columns
  - cartesian products (a join, implicit comma-join included, with no ON/USING)
  - any reference to pg_ catalog tables or information_schema

On success, rewrites the SQL: injects `LIMIT` if none is present, and fully
qualifies column references (via sqlglot's optimizer, which also resolves
aliases and raises a precise error for anything it can't resolve).
"""

from __future__ import annotations

import psycopg
import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.schema import MappingSchema

from t2sql.db.connection import get_connection
from t2sql.validation.models import ValidationError, ValidationErrorType, ValidationResult

DEFAULT_LIMIT = 1000
WIDE_TABLE_COLUMN_THRESHOLD = 20
CATALOG_SCHEMAS = {"pg_catalog", "information_schema"}

# Any of these appearing anywhere in the parsed tree -- including inside a
# CTE -- means this isn't a pure read.
FORBIDDEN_STATEMENT_TYPES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Merge,
    exp.Grant,
    exp.Command,
    exp.Copy,
    exp.Set,
    exp.Use,
)

LiveSchema = dict[str, dict[str, str]]


def _fetch_live_schema(conn: psycopg.Connection) -> LiveSchema:
    schema: LiveSchema = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public'"
        )
        for table_name, column_name, data_type in cur.fetchall():
            schema.setdefault(table_name, {})[column_name] = data_type
    return schema


def _mapping_schema(live_schema: LiveSchema, dialect: str) -> MappingSchema:
    return MappingSchema(
        {table: {col: (dtype or "text") for col, dtype in cols.items()} for table, cols in live_schema.items()},
        dialect=dialect,
    )


def _star_targets(select: exp.Select) -> list[str | None]:
    """Aliases targeted by star projections in `select`. None = bare '*'."""
    targets: list[str | None] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            targets.append(None)
        elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            targets.append(projection.table or None)
    return targets


def validate_sql(
    sql: str,
    live_schema: LiveSchema | None = None,
    conn: psycopg.Connection | None = None,
    dialect: str = "postgres",
    default_limit: int = DEFAULT_LIMIT,
) -> ValidationResult:
    if live_schema is None:
        if conn is None:
            with get_connection(role="readonly") as owned_conn:
                return validate_sql(sql, conn=owned_conn, dialect=dialect, default_limit=default_limit)
        live_schema = _fetch_live_schema(conn)

    try:
        statements = [
            s for s in sqlglot.parse(sql, dialect=dialect) if s is not None and not isinstance(s, exp.Semicolon)
        ]
    except ParseError as e:
        return ValidationResult(ok=False, errors=[ValidationError(type=ValidationErrorType.PARSE_ERROR, message=str(e))])

    if len(statements) != 1:
        return ValidationResult(
            ok=False,
            errors=[
                ValidationError(
                    type=ValidationErrorType.MULTIPLE_STATEMENTS,
                    message=f"expected exactly one SQL statement, found {len(statements)}",
                )
            ],
        )

    root = statements[0]

    if not isinstance(root, exp.Select):
        return ValidationResult(
            ok=False,
            errors=[
                ValidationError(
                    type=ValidationErrorType.NOT_A_SELECT,
                    message=f"expected a single SELECT statement, got {type(root).__name__}",
                )
            ],
        )

    errors: list[ValidationError] = []

    for node in root.find_all(*FORBIDDEN_STATEMENT_TYPES):
        errors.append(
            ValidationError(
                type=ValidationErrorType.FORBIDDEN_STATEMENT,
                message=f"statement contains a forbidden {type(node).__name__} clause: {node.sql(dialect=dialect)}",
            )
        )

    for table in root.find_all(exp.Table):
        if table.db in CATALOG_SCHEMAS or table.name.startswith("pg_"):
            errors.append(
                ValidationError(
                    type=ValidationErrorType.CATALOG_ACCESS,
                    message=f"reference to catalog table {table.sql(dialect=dialect)!r} is not allowed",
                )
            )

    scopes = list(traverse_scope(root))

    unknown_tables: set[str] = set()
    for scope in scopes:
        for source in scope.sources.values():
            if isinstance(source, exp.Table) and source.name not in live_schema:
                unknown_tables.add(source.name)
    for name in sorted(unknown_tables):
        errors.append(ValidationError(type=ValidationErrorType.UNKNOWN_TABLE, message=f"unknown table {name!r}"))

    for join in root.find_all(exp.Join):
        if join.args.get("on") is None and not join.args.get("using"):
            errors.append(
                ValidationError(
                    type=ValidationErrorType.CARTESIAN_PRODUCT,
                    message=f"join with no ON/USING condition produces a cartesian product: {join.sql(dialect=dialect)}",
                )
            )

    if not unknown_tables:
        for scope in scopes:
            if not isinstance(scope.expression, exp.Select):
                continue
            for alias in _star_targets(scope.expression):
                candidates = [scope.sources[alias]] if alias else list(scope.sources.values())
                for candidate in candidates:
                    if not isinstance(candidate, exp.Table):
                        continue
                    column_count = len(live_schema.get(candidate.name, {}))
                    if column_count > WIDE_TABLE_COLUMN_THRESHOLD:
                        errors.append(
                            ValidationError(
                                type=ValidationErrorType.WIDE_SELECT_STAR,
                                message=(
                                    f"SELECT * on {candidate.name!r}, which has {column_count} columns "
                                    f"(> {WIDE_TABLE_COLUMN_THRESHOLD}); list columns explicitly"
                                ),
                            )
                        )

    qualified_root: exp.Select | None = None
    if not unknown_tables:
        try:
            qualified_root = qualify(
                root.copy(),
                schema=_mapping_schema(live_schema, dialect),
                dialect=dialect,
                validate_qualify_columns=True,
                expand_stars=False,
                infer_schema=False,
            )
        except OptimizeError as e:
            errors.append(ValidationError(type=ValidationErrorType.UNKNOWN_COLUMN, message=str(e)))

    if errors:
        return ValidationResult(ok=False, errors=errors)

    assert qualified_root is not None
    if qualified_root.args.get("limit") is None:
        qualified_root = qualified_root.limit(default_limit)

    return ValidationResult(ok=True, errors=[], rewritten_sql=qualified_root.sql(dialect=dialect))
