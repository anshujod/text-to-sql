"""Turns the semantic layer into LLM prompt context, with retrieval that scales.

build_schema_context() selects a subset of tables (by embedding similarity to
the question, or all of them for the `all`-mode ablation baseline), expands
that selection along the join graph so bridge tables required to actually
join the selected tables are never missing, then renders DDL, column
comments, enum values, relevant metric definitions, and the house defaults
for that selection.
"""

from __future__ import annotations

from collections import deque
from typing import Literal

import psycopg
import sqlglot
from sqlglot import exp

from t2sql.db.connection import get_connection
from t2sql.retrieval.embeddings import embed_query, get_table_embeddings
from t2sql.semantic import Defaults, Entity, Metric, SemanticLayer, load_semantic_layer

RetrievalMode = Literal["all", "retrieval"]

ColumnInfo = tuple[str, str, str]  # (column_name, data_type, is_nullable)


def build_schema_context(
    question: str,
    k: int = 6,
    retrieval_mode: RetrievalMode = "retrieval",
    layer: SemanticLayer | None = None,
    conn: psycopg.Connection | None = None,
) -> str:
    layer = layer or load_semantic_layer()
    selected = select_tables(question, k=k, retrieval_mode=retrieval_mode, layer=layer)
    return _render_context(layer, selected, conn)


def select_tables(
    question: str,
    k: int = 6,
    retrieval_mode: RetrievalMode = "retrieval",
    layer: SemanticLayer | None = None,
) -> set[str]:
    layer = layer or load_semantic_layer()

    if retrieval_mode == "all":
        return set(layer.entities)

    if retrieval_mode != "retrieval":
        raise ValueError(f"unknown retrieval_mode: {retrieval_mode!r}")

    table_names, vectors = get_table_embeddings(layer)
    query_vector = embed_query(question)
    similarities = vectors @ query_vector  # vectors are normalized, so this is cosine similarity
    ranked = sorted(range(len(table_names)), key=lambda i: similarities[i], reverse=True)
    top_k = {table_names[i] for i in ranked[:k]}
    return _bridge_expand(top_k, _adjacency(layer))


def _adjacency(layer: SemanticLayer) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = {name: set() for name in layer.entities}
    for edge in layer.joins:
        adjacency[edge.from_table].add(edge.to_table)
        adjacency[edge.to_table].add(edge.from_table)
    return adjacency


def _connected_components(nodes: set[str], adjacency: dict[str, set[str]]) -> list[set[str]]:
    remaining = set(nodes)
    components: list[set[str]] = []
    while remaining:
        start = next(iter(remaining))
        component = {start}
        stack = [start]
        while stack:
            node = stack.pop()
            for neighbor in adjacency[node]:
                if neighbor in remaining and neighbor not in component:
                    component.add(neighbor)
                    stack.append(neighbor)
        components.append(component)
        remaining -= component
    return components


def _shortest_path(start: str, targets: set[str], adjacency: dict[str, set[str]]) -> list[str] | None:
    prev: dict[str, str | None] = {start: None}
    queue: deque[str] = deque([start])
    while queue:
        node = queue.popleft()
        if node in targets and node != start:
            path = []
            cursor: str | None = node
            while cursor is not None:
                path.append(cursor)
                cursor = prev[cursor]
            return path
        for neighbor in adjacency[node]:
            if neighbor not in prev:
                prev[neighbor] = node
                queue.append(neighbor)
    return None


def _bridge_expand(selected: set[str], adjacency: dict[str, set[str]]) -> set[str]:
    """Add the minimal join-graph bridge tables so `selected` is connected.

    Two selected tables that aren't directly joinable (e.g. products and
    orders) need an intermediate table (order_items) to actually be joined
    in a query. This walks the full join graph to find and add it.
    """
    selected = set(selected)
    while True:
        components = _connected_components(selected, adjacency)
        if len(components) <= 1:
            return selected
        start = next(iter(components[0]))
        other_nodes: set[str] = set().union(*components[1:])
        path = _shortest_path(start, other_nodes, adjacency)
        if path is None:
            return selected  # no path in the join graph at all; nothing more we can do
        selected |= set(path)


def _fetch_column_info(conn: psycopg.Connection, tables: set[str]) -> dict[str, list[ColumnInfo]]:
    columns: dict[str, list[ColumnInfo]] = {table: [] for table in tables}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ANY(%s) "
            "ORDER BY table_name, ordinal_position",
            (list(tables),),
        )
        for table_name, column_name, data_type, is_nullable in cur.fetchall():
            columns[table_name].append((column_name, data_type, is_nullable))
    return columns


def _render_entity(table: str, entity: Entity, columns: list[ColumnInfo]) -> str:
    col_defs = [
        f"    {name} {data_type} {'NOT NULL' if is_nullable == 'NO' else 'NULL'}"
        for name, data_type, is_nullable in columns
    ]
    ddl = f"CREATE TABLE {table} (\n" + ",\n".join(col_defs) + "\n);"

    comments = [f"COMMENT ON TABLE {table} IS '{entity.description.strip()}';"]
    for name, _, _ in columns:
        description = entity.columns.get(name)
        if description:
            comments.append(f"COMMENT ON COLUMN {table}.{name} IS '{description.strip()}';")

    notes = [f"-- grain: {entity.grain}", f"-- primary key: {entity.primary_key}"]
    for fk in entity.foreign_keys:
        notes.append(f"-- foreign key: {table}.{fk.column} -> {fk.references_table}.{fk.references_column}")
    if entity.soft_delete_column:
        notes.append(f"-- soft delete column: {entity.soft_delete_column}")
    for column, values in entity.enum_values.items():
        notes.append(f"-- enum values for {table}.{column}: {', '.join(values)}")

    return "\n".join([ddl, *comments, *notes])


def _metric_tables(metric: Metric) -> set[str]:
    try:
        parsed = sqlglot.parse_one(f"SELECT {metric.sql_expression}", dialect="postgres")
    except Exception:
        return set()
    return {column.table for column in parsed.find_all(exp.Column) if column.table}


def _render_metrics(layer: SemanticLayer, selected: set[str]) -> str | None:
    relevant = {
        name: metric for name, metric in layer.metrics.items() if _metric_tables(metric) & selected
    }
    if not relevant:
        return None
    lines = ["-- Relevant metric definitions:"]
    for name, metric in relevant.items():
        lines.append(f"-- {name}: {metric.sql_expression}")
        lines.append(f"--   {metric.description.strip()}")
        if metric.default_filters:
            lines.append(f"--   default filters: {'; '.join(metric.default_filters)}")
    return "\n".join(lines)


def _render_defaults(defaults: Defaults) -> str:
    return "\n".join(
        [
            "-- House defaults:",
            f"-- default result limit: {defaults.result_shape.default_limit}",
            f"-- exclude internal accounts by default: {defaults.scope.exclude_internal_accounts}",
            f"-- exclude soft-deleted rows by default: {defaults.scope.exclude_soft_deleted}",
            f"-- 'revenue' defaults to metric: {defaults.metric.revenue_default}",
            f"-- temporal anchor: {defaults.temporal.anchor} (not wall-clock now())",
            f"-- 'last month' means: {defaults.temporal.last_month} -- {defaults.temporal.last_month_definition}",
        ]
    )


def _render_context(layer: SemanticLayer, selected: set[str], conn: psycopg.Connection | None) -> str:
    if conn is None:
        with get_connection(role="readonly") as owned_conn:
            return _render_context(layer, selected, owned_conn)

    columns_by_table = _fetch_column_info(conn, selected)
    sections = [_render_entity(table, layer.entities[table], columns_by_table[table]) for table in sorted(selected)]

    metrics_section = _render_metrics(layer, selected)
    if metrics_section:
        sections.append(metrics_section)

    sections.append(_render_defaults(layer.defaults))
    return "\n\n".join(sections)
