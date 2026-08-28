from t2sql.eval.dataset import GoldInterpretation
from t2sql.eval.metrics import execution_accuracy


def test_exact_match_is_correct() -> None:
    gold = [GoldInterpretation(sql="SELECT COUNT(*) FROM categories")]
    assert execution_accuracy("SELECT COUNT(*) FROM categories", gold) is True


def test_semantically_equivalent_but_textually_different_sql_is_correct() -> None:
    gold = [GoldInterpretation(sql="SELECT COUNT(*) FROM categories")]
    assert execution_accuracy("SELECT COUNT(id) AS n FROM categories", gold) is True


def test_wrong_result_is_incorrect() -> None:
    gold = [GoldInterpretation(sql="SELECT COUNT(*) FROM categories")]
    assert execution_accuracy("SELECT COUNT(*) + 1 FROM categories", gold) is False


def test_order_insensitive_when_gold_has_no_order_by() -> None:
    gold = [GoldInterpretation(sql="SELECT id FROM categories WHERE id <= 3")]
    pred = "SELECT id FROM categories WHERE id <= 3 ORDER BY id DESC"
    assert execution_accuracy(pred, gold) is True


def test_order_sensitive_when_gold_has_order_by() -> None:
    gold = [GoldInterpretation(sql="SELECT id FROM categories WHERE id <= 3 ORDER BY id ASC")]
    pred_reversed = "SELECT id FROM categories WHERE id <= 3 ORDER BY id DESC"
    assert execution_accuracy(pred_reversed, gold) is False


def test_float_tolerance() -> None:
    gold = [GoldInterpretation(sql="SELECT 100.0000001::float AS x")]
    assert execution_accuracy("SELECT 100.0000002::float AS x", gold) is True


def test_float_tolerance_does_not_mask_real_differences() -> None:
    gold = [GoldInterpretation(sql="SELECT 100.0::float AS x")]
    assert execution_accuracy("SELECT 101.0::float AS x", gold) is False


def test_matches_any_of_multiple_gold_interpretations() -> None:
    gold = [GoldInterpretation(sql="SELECT 1 AS x"), GoldInterpretation(sql="SELECT 2 AS x")]
    assert execution_accuracy("SELECT 2 AS x", gold) is True
    assert execution_accuracy("SELECT 3 AS x", gold) is False


def test_invalid_pred_sql_is_incorrect_not_raising() -> None:
    gold = [GoldInterpretation(sql="SELECT COUNT(*) FROM categories")]
    assert execution_accuracy("SELECT COUNT(*) FROM not_a_real_table", gold) is False


def test_different_row_counts_is_incorrect() -> None:
    gold = [GoldInterpretation(sql="SELECT id FROM categories WHERE id <= 3")]
    pred = "SELECT id FROM categories WHERE id <= 2"
    assert execution_accuracy(pred, gold) is False
