import asyncio
import operator
from dataclasses import FrozenInstanceError, replace

import pytest

from daograph import (
    Checkpoint,
    Constraints,
    DAOGraph,
    Decision,
    Node,
    Plan,
    Situation,
    SituationEngine,
    Step,
)


def test_observation_changes_topology_and_executes_new_dependency():
    executed = []
    plans = []

    def research(ctx):
        executed.append("research")
        return {"conflict": True}

    def verify(ctx):
        executed.append("verify")
        return {"verified": True}

    def answer(ctx):
        assert ctx.state["verified"]
        executed.append("answer")
        return {"answer": "supported"}

    def assess(state, previous):
        return Situation(uncertainty=0.9 if state.get("conflict") else 0.1)

    def planner(ctx):
        if ctx.state.get("answer"):
            return Plan()
        if ctx.situation.uncertainty > 0.6:
            return Plan.chain("research", "verify", "answer")
        return Plan.chain("research", "answer")

    graph = DAOGraph(
        "Produce a supported answer",
        (Node("research", research), Node("verify", verify), Node("answer", answer)),
        planner,
        situation=SituationEngine(assess),
    )
    events = list(graph.stream({}))
    plans = [tuple(s.id for s in e.plan.steps) for e in events if e.kind == "plan"]
    assert plans[:2] == [("research", "answer"), ("research", "verify", "answer")]
    assert executed == ["research", "verify", "answer"]
    assert events[-1].result.status == "completed"


def test_replanning_drops_unfinished_task():
    executed = []

    def action(ctx):
        executed.append("observe")
        return {"enough": True}

    graph = DAOGraph(
        "Stop when enough evidence exists",
        (Node("observe", action), Node("extra", lambda ctx: executed.append("extra") or {})),
        lambda ctx: Plan() if ctx.state.get("enough") else Plan.chain("observe", "extra"),
    )
    assert graph.invoke({}).status == "completed"
    assert executed == ["observe"]


def test_dag_fan_in_and_reducers():
    graph = DAOGraph(
        "Gather and merge",
        (
            Node("one", lambda ctx: {"items": [1]}),
            Node("two", lambda ctx: {"items": [2]}),
            Node("join", lambda ctx: {"sum": sum(ctx.state["items"])}),
        ),
        lambda ctx: Plan((Step("a", "one"), Step("b", "two"), Step("c", "join", ("a", "b")))),
        reducers={"items": operator.add},
    )
    result = graph.invoke({"items": []})
    assert result.status == "completed"
    assert result.completed == ("a", "b", "c")
    assert result.state["sum"] == 3


def test_same_node_can_repeat_with_fresh_step_id_and_hits_budget():
    graph = DAOGraph(
        "Explore within a budget",
        (Node("search", lambda ctx: {"calls": ctx.state.get("calls", 0) + 1}),),
        lambda ctx: Plan((Step(f"search:{ctx.steps}", "search"),)),
        constraints=Constraints(max_steps=3),
    )
    result = graph.invoke({})
    assert result.status == "blocked"
    assert result.state["calls"] == 3
    assert len(result.history) == 3


def _approval_graph(calls, constraints=None, assessor=None):
    def prepare(ctx):
        calls.append("prepare")
        return {"draft": "ready"}

    def execute(ctx):
        calls.append("execute")
        return {"receipt": "ok"}

    return DAOGraph(
        "Execute only after approval",
        (Node("prepare", prepare), Node("execute", execute, side_effect=True, reversibility=0.1)),
        lambda ctx: Plan.chain("prepare", "execute"),
        constraints=constraints or Constraints(),
        situation=SituationEngine(assessor) if assessor else SituationEngine(),
    )


def test_approval_json_roundtrip_does_not_replay_completed_tasks():
    calls = []
    graph = _approval_graph(calls)
    paused = graph.invoke({"approved": True})
    assert paused.status == "paused"  # Writable facts cannot grant authority.
    assert calls == ["prepare"]
    restored = Checkpoint.from_json(paused.checkpoint.to_json())
    result = graph.resume(restored, approval=restored.interrupt.id)
    assert result.status == "completed"
    assert calls == ["prepare", "execute"]
    assert result.completed == ("prepare", "execute")


def test_wrong_approval_and_changed_configuration_are_rejected():
    graph = _approval_graph([])
    checkpoint = graph.invoke({}).checkpoint
    with pytest.raises(ValueError, match="exact checkpoint"):
        graph.resume(checkpoint, approval="anything")
    changed = replace(graph, goal="A different goal")
    with pytest.raises(ValueError, match="different graph"):
        changed.resume(checkpoint, approval=checkpoint.interrupt.id)
    changed = replace(graph, revision="2")
    with pytest.raises(ValueError, match="different graph"):
        changed.resume(checkpoint, approval=checkpoint.interrupt.id)


def test_changed_situation_invalidates_old_approval():
    calls = []
    evaluations = 0

    def assess(state, previous):
        nonlocal evaluations
        evaluations += 1
        return Situation(risk=0.8 if evaluations < 3 else 0.95)

    graph = _approval_graph(calls, assessor=assess)
    saved = graph.invoke({}).checkpoint
    result = graph.resume(saved, approval=saved.interrupt.id)
    assert result.status == "paused"
    assert result.checkpoint.interrupt.id != saved.interrupt.id
    assert calls == ["prepare"]


def test_hard_block_wins_over_approval_and_is_rechecked_on_resume():
    calls = []
    revoked = False

    def rule(ctx, node):
        return Decision.block("Permission revoked") if revoked else Decision.allow()

    graph = _approval_graph(calls, constraints=Constraints(rules=(rule,)))
    checkpoint = graph.invoke({}).checkpoint
    revoked = True
    result = graph.resume(checkpoint, approval=checkpoint.interrupt.id)
    assert result.status == "blocked"
    assert calls == ["prepare"]


def test_each_side_effect_needs_its_own_approval():
    calls = []
    graph = DAOGraph(
        "Approve each effect",
        tuple(
            Node(name, lambda ctx, n=name: calls.append(n) or {}, requires_approval=True)
            for name in ("first", "second")
        ),
        lambda ctx: Plan.chain("first", "second"),
    )
    first = graph.invoke({}).checkpoint
    second = graph.resume(first, approval=first.interrupt.id)
    assert second.status == "paused"
    assert calls == ["first"]
    assert second.checkpoint.interrupt.step.node == "second"


def test_read_only_research_is_allowed_under_high_risk():
    graph = DAOGraph(
        "Investigate",
        (Node("research", lambda ctx: {"done": True}),),
        lambda ctx: Plan.chain("research"),
    )
    assert graph.invoke({"signals": {"risk": 0.99, "reversibility": 0.01}}).status == "completed"


def test_node_metadata_cannot_be_diluted_by_low_situation_risk():
    calls = []
    graph = DAOGraph(
        "Gate inherently risky actions",
        (Node("effect", lambda ctx: calls.append(1) or {}, side_effect=True, risk=0.95),),
        lambda ctx: Plan.chain("effect"),
    )
    assert graph.invoke({"signals": {"risk": 0.0}}).status == "paused"
    assert calls == []


def test_allowlist_blocks_before_tool_execution():
    calls = []
    graph = DAOGraph(
        "Restrict capabilities",
        (Node("tool", lambda ctx: calls.append(1) or {}),),
        lambda ctx: Plan.chain("tool"),
        constraints=Constraints(allowed_nodes=frozenset()),
    )
    assert graph.invoke({}).status == "blocked"
    assert calls == []


def test_state_and_goal_are_immutable_and_input_is_isolated():
    source = {"items": [{"value": 1}]}

    def action(ctx):
        with pytest.raises(TypeError):
            ctx.state["items"][0]["value"] = 2
        with pytest.raises(FrozenInstanceError):
            ctx.goal = "change goal"
        return {"done": True}

    graph = DAOGraph("Fixed goal", (Node("action", action),), lambda ctx: Plan.chain("action"))
    with pytest.raises(FrozenInstanceError):
        graph.constraints = Constraints(max_steps=100)
    result = graph.invoke(source)
    assert result.status == "completed"
    assert source == {"items": [{"value": 1}]}
    with pytest.raises(TypeError):
        result.state["items"][0]["value"] = 3


@pytest.mark.parametrize(
    "planner",
    [
        lambda ctx: Plan.chain("missing"),
        lambda ctx: Plan((Step("task", "action", ("missing",)),)),
        lambda ctx: "not a plan",
        lambda ctx: Plan((Step("a", "action", ("b",)), Step("b", "action", ("a",)))),
    ],
)
def test_invalid_plans_fail_without_executing_tools(planner):
    calls = []
    graph = DAOGraph(
        "Validate graph", (Node("action", lambda ctx: calls.append(1) or {}),), planner
    )
    result = graph.invoke({})
    assert result.status == "failed"
    assert calls == []


def test_completed_steps_cannot_be_redefined():
    graph = DAOGraph(
        "Keep history stable",
        (Node("first", lambda ctx: {}), Node("second", lambda ctx: {})),
        lambda ctx: Plan((Step("fixed", "second" if ctx.steps else "first"),)),
    )
    result = graph.invoke({})
    assert result.status == "failed"
    assert "cannot be redefined" in result.reason
    assert result.completed == ("fixed",)


def test_dependency_on_completed_step_can_be_outside_new_plan():
    graph = DAOGraph(
        "Reuse completed evidence",
        (Node("action", lambda ctx: {}),),
        lambda ctx: (
            Plan((Step("b", "action", ("a",)),)) if ctx.steps else Plan((Step("a", "action"),))
        ),
    )
    result = graph.invoke({})
    assert result.status == "completed"
    assert result.completed == ("a", "b")


def test_failed_reducer_does_not_partially_commit_patch():
    def broken(old, new):
        raise RuntimeError("reducer failed")

    graph = DAOGraph(
        "Atomic state commit",
        (Node("action", lambda ctx: {"first": 2, "items": [2]}),),
        lambda ctx: Plan.chain("action"),
        reducers={"items": broken},
    )
    result = graph.invoke({"first": 1, "items": [1]})
    assert result.status == "failed"
    assert result.state["first"] == 1
    assert result.state["items"] == (1,)
    assert result.history == ()


def test_node_failure_is_observable_and_never_automatically_retried():
    calls = []

    def broken(ctx):
        calls.append(1)
        raise RuntimeError("tool unavailable")

    graph = DAOGraph("Handle failures", (Node("broken", broken),), lambda ctx: Plan.chain("broken"))
    events = list(graph.stream({}))
    assert events[-1].kind == "failed"
    assert "tool unavailable" in events[-1].result.reason
    assert calls == [1]


def test_stream_yields_before_running_action_and_can_be_closed():
    calls = []
    graph = DAOGraph(
        "Stream incrementally",
        (Node("action", lambda ctx: calls.append(1) or {}),),
        lambda ctx: Plan.chain("action"),
    )
    stream = graph.stream({})
    assert next(stream).kind == "situation"
    assert next(stream).kind == "plan"
    assert next(stream).kind == "node_start"
    assert calls == []
    stream.close()
    assert calls == []


def test_async_callbacks_reducers_and_approval():
    async def action(ctx):
        await asyncio.sleep(0)
        return {"items": [2]}

    async def planner(ctx):
        return Plan.chain("action")

    async def assess(state, previous):
        return Situation()

    async def reducer(old, new):
        return old + new

    async def run():
        graph = DAOGraph(
            "Use async callbacks",
            (Node("action", action, requires_approval=True),),
            planner,
            situation=SituationEngine(assess),
            reducers={"items": reducer},
        )
        result = await graph.ainvoke({"items": [1]})
        result = await graph.aresume(result.checkpoint, approval=result.checkpoint.interrupt.id)
        assert result.status == "completed"
        assert result.state["items"] == (1, 2)
        with pytest.raises(RuntimeError, match="async API"):
            graph.invoke({})

    asyncio.run(run())


def test_concurrent_invocations_do_not_share_history_or_facts():
    async def action(ctx):
        await asyncio.sleep(0)
        return {"result": ctx.state["input"] * 2}

    graph = DAOGraph(
        "Independent runs", (Node("double", action),), lambda ctx: Plan.chain("double")
    )

    async def run():
        results = await asyncio.gather(*(graph.ainvoke({"input": n}) for n in range(5)))
        assert [r.state["result"] for r in results] == [0, 2, 4, 6, 8]
        assert all(r.completed == ("double",) for r in results)

    asyncio.run(run())


def test_json_contract_rejects_non_finite_and_nonserializable_state():
    graph = DAOGraph("Validate state", (), lambda ctx: Plan())
    with pytest.raises(ValueError, match="finite"):
        graph.invoke({"value": float("nan")})
    with pytest.raises(TypeError, match="JSON"):
        graph.invoke({"value": object()})
    with pytest.raises(TypeError, match="keys"):
        graph.invoke({1: "invalid"})


def test_plan_mermaid_handles_external_completed_dependencies():
    diagram = Plan((Step("answer:1", "answer", ("research:1",)),)).to_mermaid()
    assert "accTitle:" in diagram
    assert "completed_0 --> task_0" in diagram
    assert "research:1 (completed)" in diagram
