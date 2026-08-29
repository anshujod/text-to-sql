"""Task 2.1 gate: all 7 ambiguity types are encoded with a policy, an
example question, and detection hints.
"""

from t2sql.clarify.taxonomy import TAXONOMY, AmbiguityType, ClarificationPolicy, get_spec


def test_all_seven_types_are_covered() -> None:
    assert set(TAXONOMY.keys()) == set(AmbiguityType)
    assert len(TAXONOMY) == 7


def test_every_spec_has_a_policy_example_and_hints() -> None:
    for ambiguity_type, spec in TAXONOMY.items():
        assert spec.type == ambiguity_type
        assert spec.default_policy in ClarificationPolicy
        assert spec.example_question.strip()
        assert spec.description.strip()
        assert len(spec.detection_hints) > 0


def test_get_spec_looks_up_by_type() -> None:
    spec = get_spec(AmbiguityType.METRIC)
    assert spec.type == AmbiguityType.METRIC
    assert spec.default_policy == ClarificationPolicy.ASK
