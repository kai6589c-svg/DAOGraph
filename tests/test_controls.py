import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from daograph import (
    Checkpoint,
    Constraints,
    DAOGraph,
    Decision,
    GoalCheck,
    Node,
    Plan,
    PlanChange,
    ReplanDecision,
    Signal,
    Situation,
    Step,
)


def legacy_action(context):
    return {"receipt": "ok"}


def legacy_planner(context):
    return Plan.chain("write")


def test_actual_old_checkpoint_restores_with_default_controls():
    graph = DAOGraph(
        "Legacy approval", (Node("write", legacy_action, requires_approval=True),), legacy_planner
    )
    saved = Checkpoint.from_json(
        (Path(__file__).parent / "fixtures/v0.1.0-approval.json").read_text()
    )
    result = graph.resume(saved, approval=saved.interrupt.id)
    assert result.status == "completed"
    assert result.state["receipt"] == "ok"


def test_selective_reuse_and_forced_exhausted_plan():
    planned = []
    adapted = []

    def planner(ctx):
        planned.append(ctx.completed)
        return Plan.chain("first", "second") if ctx.steps < 2 else Plan()

    def adapt(ctx, previous, plan):
        adapted.append(previous)
        return ReplanDecision(False, "No relevant change")

    graph = DAOGraph(
        "Reuse work",
        (Node("first", lambda ctx: {}), Node("second", lambda ctx: {})),
        planner,
        adapt=adapt,
    )
    events = list(graph.stream({}))
    assert planned == [(), ("first", "second")]
    assert len(adapted) == 1
    assert isinstance(adapted[0], Situation)
    assert [e.kind for e in events].count("plan_reused") == 1
    assert events[-1].result.status == "completed"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_goal_stops_unfinished_work_and_sync_async_controls(asynchronous):
    def planner(ctx):
        return Plan.chain("observe", "extra")

    def verifier(ctx):
        return GoalCheck(bool(ctx.state.get("done")), "Evidence complete")

    def adapter(ctx, previous, plan):
        return ReplanDecision(False, "Retain")

    async def async_planner(ctx):
        return planner(ctx)

    async def async_verifier(ctx):
        return verifier(ctx)

    async def async_adapter(ctx, previous, plan):
        return adapter(ctx, previous, plan)

    graph = DAOGraph(
        "Finish early",
        (
            Node("observe", lambda ctx: {"done": True}),
            Node("extra", lambda ctx: pytest.fail("Unnecessary action")),
        ),
        async_planner if asynchronous else planner,
        adapt=async_adapter if asynchronous else adapter,
        verify_goal=async_verifier if asynchronous else verifier,
    )
    result = asyncio.run(graph.ainvoke({})) if asynchronous else graph.invoke({})
    assert result.completed == ("observe",)
    assert result.goal_check == GoalCheck(True, "Evidence complete")
    # Exercise adapters as well as the early-stop path.
    reused = replace(graph, verify_goal=lambda ctx: GoalCheck(ctx.steps == 1, "Stop"))
    assert reused.invoke({}).status == "completed"


def test_async_adapter_reuses_before_goal_satisfaction():
    async def adapt(ctx, previous, plan):
        return ReplanDecision(False, "Retain")

    graph = DAOGraph(
        "Two steps",
        (Node("a", lambda ctx: {}), Node("b", lambda ctx: {})),
        lambda ctx: Plan.chain("a", "b"),
        adapt=adapt,
        verify_goal=lambda ctx: GoalCheck(ctx.steps == 2, "Done"),
    )
    assert graph.invoke({}).completed == ("a", "b")


def test_initial_goal_needs_no_planner_and_empty_unmet_goal_is_incomplete():
    graph = DAOGraph(
        "Check",
        (),
        lambda ctx: pytest.fail("Already satisfied"),
        verify_goal=lambda ctx: GoalCheck(True, "Already satisfied"),
    )
    assert graph.invoke({}).completed == ()
    graph = replace(
        graph,
        planner=lambda ctx: Plan(),
        verify_goal=lambda ctx: GoalCheck(False, "Missing evidence"),
    )
    events = list(graph.stream({}))
    assert events[-1].kind == "incomplete"
    assert events[-1].result.reason == "Missing evidence"


def test_reused_actions_recheck_constraints():
    def rule(ctx, node):
        return Decision.block("Revoked") if ctx.steps else Decision.allow()

    graph = DAOGraph(
        "Recheck",
        (Node("a", lambda ctx: {}), Node("b", lambda ctx: pytest.fail("Denied"))),
        lambda ctx: Plan.chain("a", "b"),
        constraints=Constraints(rules=(rule,)),
        adapt=lambda ctx, previous, plan: ReplanDecision(False, "Retain"),
    )
    assert graph.invoke({}).status == "blocked"


def test_plan_changes_exclude_history_and_report_sorted_differences():
    old = Plan((Step("done", "a"), Step("z", "a"), Step("change", "a")))
    new = Plan((Step("done", "a"), Step("b", "b"), Step("change", "b")))
    assert PlanChange.between(old, new, ("done",)) == PlanChange(("b",), ("z",), ("change",))
    graph = DAOGraph(
        "Trace",
        (Node("a", lambda ctx: {}), Node("b", lambda ctx: {})),
        lambda ctx: new if ctx.steps else old,
        constraints=Constraints(max_steps=1),
    )
    changes = [e.plan_change for e in graph.stream({}) if e.kind == "plan"]
    assert changes[1] == PlanChange(("b",), ("z",), ("change",))


@pytest.mark.parametrize("field, bad", [("adapt", False), ("verify_goal", False)])
def test_invalid_configuration(field, bad):
    with pytest.raises(TypeError, match="callable"):
        DAOGraph("Invalid", (), lambda ctx: Plan(), **{field: bad})


@pytest.mark.parametrize("record", [ReplanDecision, GoalCheck])
def test_decision_record_validation(record):
    with pytest.raises(TypeError):
        record(1, "Invalid boolean")
    with pytest.raises(TypeError):
        record(True, None)


def test_malformed_control_results_fail_before_dispatch():
    graph = DAOGraph("Invalid", (), lambda ctx: Plan(), verify_goal=lambda ctx: True)
    assert graph.invoke({}).status == "failed"
    graph = DAOGraph(
        "Invalid",
        (Node("a", lambda ctx: {}), Node("b", lambda ctx: {})),
        lambda ctx: Plan.chain("a", "b"),
        adapt=lambda *args: False,
    )
    assert graph.invoke({}).completed == ("a",)
    assert graph.invoke({}).status == "failed"


def test_resume_runs_exact_pending_before_new_control_callbacks():
    calls = []
    enabled = False

    def verify(ctx):
        calls.append("verify")
        return GoalCheck(enabled, "Outcome now sufficient")

    graph = DAOGraph(
        "Approval",
        (Node("effect", lambda ctx: calls.append("effect") or {}, requires_approval=True),),
        lambda ctx: Plan.chain("effect"),
        adapt=lambda *args: pytest.fail("Exhaustion forces planning"),
        verify_goal=verify,
    )
    saved = graph.invoke({}).checkpoint
    calls.clear()
    enabled = True
    assert graph.resume(saved, approval=saved.interrupt.id).status == "completed"
    assert calls == ["effect", "verify"]
    with pytest.raises(ValueError, match="different graph"):
        replace(graph, verify_goal=None).resume(saved, approval=saved.interrupt.id)


def test_signal_roundtrip_unknowns_and_old_payloads():
    signal = Signal("coverage", None, ("source:one",))
    situation = Situation(signals=(signal, Signal("conflict", 0)))
    assert Situation.from_dict(situation.to_dict()) == situation
    assert Situation.from_dict({"risk": 0.1}).signals == ()
    assert "signals" not in Situation().to_dict()
    with pytest.raises(ValueError, match="unique"):
        Situation(signals=(signal, signal))
    with pytest.raises(TypeError):
        Situation(signals=("invalid",))


@pytest.mark.parametrize(
    "name,value,evidence",
    [
        ("not valid", 0, ()),
        ("coverage", True, ()),
        ("coverage", float("nan"), ()),
        ("coverage", 2, ()),
        ("coverage", 0, "source"),
        ("coverage", 0, (1,)),
    ],
)
def test_signal_rejects_invalid_data(name, value, evidence):
    with pytest.raises(ValueError):
        Signal(name, value, evidence)


def test_goal_callback_cancellation_propagates_and_malformed_result_keeps_typed_fields():
    async def verify(ctx):
        raise asyncio.CancelledError

    graph = DAOGraph("Cancel", (), lambda ctx: Plan(), verify_goal=verify)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(graph.ainvoke({}))
    malformed = replace(graph, verify_goal=lambda ctx: True).invoke({})
    assert malformed.status == "failed" and malformed.goal_check is None
    assert isinstance(malformed.plan, Plan) and isinstance(malformed.situation, Situation)
