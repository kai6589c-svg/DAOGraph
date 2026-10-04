import pytest

from daograph import Constraints, DAOGraph, Node, Plan, Situation, SituationEngine


@pytest.mark.parametrize("score", [-0.1, 1.1, float("nan"), float("inf"), True, "high"])
def test_invalid_scores_are_rejected(score):
    with pytest.raises(ValueError):
        Situation(risk=score)


def test_labels_and_serialization():
    situation = Situation(risk=0.9, uncertainty=0.8, reversibility=0.1, evidence=["API response"])
    assert set(situation.labels) == {"high_risk", "uncertain", "hard_to_reverse"}
    assert situation.evidence == ("API response",)
    assert Situation.from_dict(situation.to_dict()) == situation


def test_default_engine_reads_explicit_signals_and_uses_defaults():
    engine = SituationEngine()
    result = engine.assess({"signals": {"risk": 0.8}}, None)
    assert result.risk == 0.8
    assert result.uncertainty == 0.5
    assert engine.assess({}, result).risk == 0.0


def test_bad_assessment_fails_before_planning_or_execution():
    calls = []
    graph = DAOGraph(
        "Require a valid assessment",
        (Node("action", lambda ctx: calls.append(1) or {}),),
        lambda ctx: Plan.chain("action"),
        situation=SituationEngine(lambda state, previous: "high risk"),
    )
    assert graph.invoke({}).status == "failed"
    assert calls == []


@pytest.mark.parametrize("steps", [0, -1, True, 2.5])
def test_invalid_budgets_are_rejected(steps):
    with pytest.raises(ValueError):
        Constraints(max_steps=steps)


def test_bad_rules_fail_closed():
    graph = DAOGraph(
        "Require valid policy output",
        (Node("action", lambda ctx: {"executed": True}),),
        lambda ctx: Plan.chain("action"),
        constraints=Constraints(rules=(lambda ctx, node: True,)),
    )
    result = graph.invoke({})
    assert result.status == "failed"
    assert "executed" not in result.state
