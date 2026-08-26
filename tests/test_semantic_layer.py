import pytest
from pydantic import ValidationError

from t2sql.db.connection import get_connection
from t2sql.semantic.loader import (
    SEMANTIC_DIR,
    SemanticLayerValidationError,
    _read_yaml,
    load_semantic_layer,
    validate_semantic_layer,
)
from t2sql.semantic.models import SemanticLayer


def _raw_semantic_layer_dict() -> dict:
    return {
        "metrics": _read_yaml(SEMANTIC_DIR / "metrics.yaml"),
        "entities": _read_yaml(SEMANTIC_DIR / "entities.yaml"),
        "joins": _read_yaml(SEMANTIC_DIR / "joins.yaml"),
        "defaults": _read_yaml(SEMANTIC_DIR / "defaults.yaml"),
    }


def test_semantic_layer_loads_and_validates_against_live_schema() -> None:
    layer = load_semantic_layer()
    with get_connection(role="readonly") as conn:
        validate_semantic_layer(layer, conn)  # must not raise


def test_broken_metric_default_reference_fails_at_load_time() -> None:
    data = _raw_semantic_layer_dict()
    data["defaults"]["metric"]["revenue_default"] = "does_not_exist"
    with pytest.raises(ValidationError, match="does_not_exist"):
        SemanticLayer.model_validate(data)


def test_broken_foreign_key_table_fails_at_load_time() -> None:
    data = _raw_semantic_layer_dict()
    data["entities"]["users"]["foreign_keys"][0]["references_table"] = "not_a_table"
    with pytest.raises(ValidationError, match="not_a_table"):
        SemanticLayer.model_validate(data)


def test_broken_column_reference_in_metric_fails_live_validation() -> None:
    data = _raw_semantic_layer_dict()
    data["metrics"]["order_count"]["sql_expression"] = "COUNT(orders.nonexistent_column)"
    layer = SemanticLayer.model_validate(data)  # structurally fine, no DB needed yet

    with get_connection(role="readonly") as conn:
        with pytest.raises(SemanticLayerValidationError, match="nonexistent_column"):
            validate_semantic_layer(layer, conn)


def test_broken_column_reference_in_entity_fails_live_validation() -> None:
    data = _raw_semantic_layer_dict()
    data["entities"]["customers"]["columns"]["not_a_real_column"] = "bogus"
    layer = SemanticLayer.model_validate(data)

    with get_connection(role="readonly") as conn:
        with pytest.raises(SemanticLayerValidationError, match="not_a_real_column"):
            validate_semantic_layer(layer, conn)
