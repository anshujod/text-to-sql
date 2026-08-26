"""Loads the semantic layer YAML and validates it against the live database.

Two validation layers:
1. Structural/internal consistency (SemanticLayer's pydantic model_validator)
   -- runs at load time, no DB needed.
2. Live-schema validation (validate_semantic_layer) -- every table/column
   referenced by entities, joins, and metric sql_expressions must exist in
   the connected Postgres database, and every sql_expression must parse
   under sqlglot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import sqlglot
import yaml
from sqlglot import exp

from t2sql.db.connection import get_connection
from t2sql.semantic.models import SemanticLayer

SEMANTIC_DIR = Path(__file__).parent


class SemanticLayerValidationError(ValueError):
    """Raised when the semantic layer doesn't match the live database."""


def _read_yaml(path: Path) -> Any:
    with open(path) as f:
        return yaml.safe_load(f)


def load_semantic_layer(base_dir: Path | None = None) -> SemanticLayer:
    base_dir = base_dir or SEMANTIC_DIR
    data = {
        "metrics": _read_yaml(base_dir / "metrics.yaml"),
        "entities": _read_yaml(base_dir / "entities.yaml"),
        "joins": _read_yaml(base_dir / "joins.yaml"),
        "defaults": _read_yaml(base_dir / "defaults.yaml"),
    }
    return SemanticLayer.model_validate(data)


def _fetch_live_schema(conn: psycopg.Connection) -> dict[str, set[str]]:
    schema: dict[str, set[str]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public'"
        )
        for table_name, column_name in cur.fetchall():
            schema.setdefault(table_name, set()).add(column_name)
    return schema


def _check_expression_columns(
    label: str, sql_expression: str, live_schema: dict[str, set[str]], errors: list[str]
) -> None:
    try:
        parsed = sqlglot.parse_one(f"SELECT {sql_expression}", dialect="postgres")
    except Exception as e:  # sqlglot raises its own ParseError subclasses
        errors.append(f"{label}: sql_expression failed to parse under sqlglot: {e}")
        return

    for column in parsed.find_all(exp.Column):
        table = column.table
        col_name = column.name
        if not table:
            errors.append(
                f"{label}: sql_expression has unqualified column {col_name!r}; "
                "qualify it as table.column so it can be validated"
            )
            continue
        if table not in live_schema:
            errors.append(f"{label}: sql_expression references unknown table {table!r}")
        elif col_name not in live_schema[table]:
            errors.append(f"{label}: sql_expression references unknown column {table}.{col_name}")


def validate_semantic_layer(
    layer: SemanticLayer, conn: psycopg.Connection | None = None
) -> None:
    """Validate every table/column reference against the live database.

    Raises SemanticLayerValidationError with every problem found (not just
    the first) if anything doesn't match.
    """
    if conn is None:
        with get_connection(role="readonly") as owned_conn:
            validate_semantic_layer(layer, owned_conn)
        return

    live_schema = _fetch_live_schema(conn)
    errors: list[str] = []

    for entity_name, entity in layer.entities.items():
        if entity_name not in live_schema:
            errors.append(f"entity {entity_name!r}: table does not exist in the live database")
            continue
        live_columns = live_schema[entity_name]
        for col in entity.columns:
            if col not in live_columns:
                errors.append(
                    f"entity {entity_name!r}: column {col!r} does not exist in the live database"
                )
        for fk in entity.foreign_keys:
            if fk.references_table in live_schema and fk.references_column not in live_schema[fk.references_table]:
                errors.append(
                    f"entity {entity_name!r}: foreign key {fk.column!r} references "
                    f"{fk.references_table}.{fk.references_column}, which does not exist"
                )

    for edge in layer.joins:
        for table, column in ((edge.from_table, edge.from_column), (edge.to_table, edge.to_column)):
            if table not in live_schema:
                errors.append(f"join edge: table {table!r} does not exist in the live database")
            elif column not in live_schema[table]:
                errors.append(f"join edge: column {table}.{column} does not exist in the live database")

    for metric_name, metric in layer.metrics.items():
        _check_expression_columns(f"metric {metric_name!r}", metric.sql_expression, live_schema, errors)
        for i, filt in enumerate(metric.default_filters):
            _check_expression_columns(
                f"metric {metric_name!r} default_filters[{i}]", f"1 WHERE {filt}", live_schema, errors
            )

    if errors:
        raise SemanticLayerValidationError(
            "Semantic layer failed validation against the live database:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
