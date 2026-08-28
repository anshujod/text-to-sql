from t2sql.db.connection import get_connection
from t2sql.retrieval.context import _adjacency, _bridge_expand, build_schema_context, select_tables
from t2sql.semantic import load_semantic_layer


def test_bridge_expand_connects_tables_with_no_direct_join() -> None:
    layer = load_semantic_layer()
    adjacency = _adjacency(layer)

    # products and orders have no direct join edge -- order_items bridges them.
    expanded = _bridge_expand({"products", "orders"}, adjacency)

    assert expanded == {"products", "order_items", "orders"}


def test_bridge_expand_is_noop_when_already_connected() -> None:
    layer = load_semantic_layer()
    adjacency = _adjacency(layer)

    expanded = _bridge_expand({"orders", "order_items"}, adjacency)

    assert expanded == {"orders", "order_items"}


def test_retrieval_mode_all_selects_every_table() -> None:
    layer = load_semantic_layer()

    selected = select_tables("anything", retrieval_mode="all", layer=layer)

    assert selected == set(layer.entities)


def test_retrieval_selects_expected_tables_for_customer_spend_question() -> None:
    layer = load_semantic_layer()

    selected = select_tables("which customers spent the most last month", k=6, layer=layer)

    assert "customers" in selected
    assert "orders" in selected
    assert "order_items" in selected
    assert "products" not in selected
    assert "categories" not in selected
    assert "addresses" not in selected


def test_retrieval_selects_catalog_tables_for_category_question() -> None:
    layer = load_semantic_layer()

    selected = select_tables("what categories do we sell products in", k=6, layer=layer)

    assert "products" in selected
    assert "categories" in selected
    assert "sessions" not in selected


def test_retrieval_selects_sessions_for_engagement_question() -> None:
    layer = load_semantic_layer()

    selected = select_tables("how many active sessions per day", k=6, layer=layer)

    assert "sessions" in selected
    assert "categories" not in selected
    assert "addresses" not in selected


def test_unknown_retrieval_mode_raises() -> None:
    layer = load_semantic_layer()
    try:
        select_tables("irrelevant", retrieval_mode="bogus", layer=layer)  # type: ignore[arg-type]
    except ValueError as e:
        assert "bogus" in str(e)
    else:
        raise AssertionError("expected ValueError for unknown retrieval_mode")


def test_build_schema_context_renders_ddl_and_defaults_for_selected_tables() -> None:
    layer = load_semantic_layer()

    with get_connection(role="readonly") as conn:
        context = build_schema_context(
            "which customers spent the most last month", k=6, layer=layer, conn=conn
        )

    assert "CREATE TABLE customers" in context
    assert "CREATE TABLE orders" in context
    assert "CREATE TABLE order_items" in context
    assert "CREATE TABLE products" not in context
    assert "House defaults" in context
    assert "temporal anchor: max_order_created_at" in context


def test_build_schema_context_all_mode_includes_every_table() -> None:
    layer = load_semantic_layer()

    with get_connection(role="readonly") as conn:
        context = build_schema_context("anything", retrieval_mode="all", layer=layer, conn=conn)

    for table_name in layer.entities:
        assert f"CREATE TABLE {table_name}" in context
