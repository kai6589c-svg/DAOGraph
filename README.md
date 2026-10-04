# DAOGraph

_Situation-driven adaptive agent graphs with minimal constraints. Python 3.11+, MIT, alpha 0.1.0._

---

DAOGraph turns observations into a **Situation**, asks a planner to compose the
current execution DAG, executes one ready task, and repeats. A new observation
can insert verification, remove unnecessary work, or replace the remaining path.
The host fixes the goal, registered capabilities, and execution boundaries.

The package has three implementation modules and no runtime dependencies:

| Module | Responsibility |
| --- | --- |
| `graph` | Adaptive DAGs, state updates, streaming, approval checkpoints |
| `situation` | Six situation signals, evidence, pluggable assessment |
| `constraints` | Step limit, capability allowlist, approval, custom hard rules |

[中文说明](README.zh-CN.md) · [Architecture](docs/architecture.md) ·
[API reference](docs/api.md) · [Release guide](docs/releasing.md)

## 🚀 Install and run

Install from a local checkout; this version has not been published to PyPI:

```bash
python -m pip install -e .
python examples/adaptive_research.py
python examples/approval.py
python examples/async_tools.py
```

For development:

```bash
python -m pip install -e '.[dev]'
pytest
ruff check .
ruff format --check .
mypy
python -m build
python -m twine check dist/*
```

## 🎯 A small working agent

A planner returns the DAG for the current situation. Completed task ids keep their
identity across replans. Each node returns a partial state update.

```python
from daograph import Context, DAOGraph, Node, Plan, Situation, SituationEngine


def assess(state, previous):
    conflict = state.get("conflict", False) and not state.get("verified", False)
    return Situation(
        uncertainty=0.85 if conflict else 0.1,
        evidence=("Conflicting observations" if conflict else "Evidence resolved",),
    )


def research(ctx: Context):
    return {"conflict": True, "sources": ["A", "B"]}


def verify(ctx: Context):
    return {"verified": True}


def answer(ctx: Context):
    return {"answer": "Checked against both sources"}


def planner(ctx: Context):
    if ctx.state.get("answer"):
        return Plan(reason="Answer delivered")
    if ctx.situation.uncertainty >= 0.6 or ctx.state.get("verified"):
        return Plan.chain("research", "verify", "answer", reason="Verify the evidence")
    return Plan.chain("research", "answer", reason="Start with a short path")


graph = DAOGraph(
    goal="Give a supported answer",
    nodes=(Node("research", research), Node("verify", verify), Node("answer", answer)),
    planner=planner,
    situation=SituationEngine(assess),
)
result = graph.invoke({"question": "Which source is correct?"})
assert result.status == "completed"
assert result.completed == ("research", "verify", "answer")
print(result.state["answer"])
```

The demo deliberately uses deterministic observations. Replace the callback bodies
with your search, model, database, or other tool calls. A planner or assessor may
also be async and model-backed; the core does not depend on a model provider.

## 🔄 The control loop

```mermaid
flowchart LR
    accTitle: DAOGraph adaptive control loop
    accDescr: Host-defined goals and constraints govern a loop that assesses facts, composes a transient DAG, checks one action, executes it, and observes feedback.

    goal["Host goal"] --> plan["Compose current DAG"]
    facts["Observe facts"] --> situation["Assess situation"]
    situation --> plan
    plan --> gate{"Check constraints"}
    constraints["Host boundaries"] --> gate
    gate -->|Allow| action["Execute one task"]
    gate -->|Pause| approval["Host approval"]
    approval -->|Recheck| gate
    gate -->|Block| stop(["Stop run"])
    action --> facts
```

An assessment happens before the first task and after every successful task. The
planner is rerun after each assessment. The runtime takes the first ready task in
plan order, commits its update, and discards the unfinished plan before composing
again. Completed task ids prevent unintentional replay during replanning.

DAG roots are executed sequentially in 0.1.0. Fan-in dependencies are supported;
parallel execution within one run is outside this release. Independent async runs
can execute concurrently.

## 📊 Situation

| Signal | Default | Interpretation of larger values |
| --- | ---: | --- |
| `risk` | 0.0 | Greater estimated harm |
| `uncertainty` | 0.5 | Less reliable knowledge |
| `urgency` | 0.0 | More time pressure |
| `reversibility` | 1.0 | Easier to undo |
| `resource_pressure` | 0.0 | Fewer remaining resources |
| `trust` | 0.5 | More trusted evidence |

All scores must be finite numbers in `[0, 1]`. `evidence` records the reasons for
an assessment. The default engine reads the complete `state["signals"]` mapping;
missing fields use the defaults above. It does not infer risk from arbitrary text.
Supply a domain assessor to interpret facts and history. Scores and label
thresholds are explicit estimates, without a claim of statistical calibration.

The planner decides how each signal affects topology. The runtime does not assume
that increasing urgency permits bypassing a constraint, or that increasing
uncertainty always warrants another expensive model call.

## 🛡️ Minimal constraints and approval

`Constraints()` supplies a 32-task execution limit. `allowed_nodes` optionally
narrows the registered capabilities. `rules` adds small synchronous callbacks
that return `Decision.allow()`, `.pause(reason)`, or `.block(reason)`.

A node marked `side_effect=True` pauses if effective risk is at least `0.7` or
effective reversibility is at most `0.3`. Effective risk is the larger of the
situation and node risk; effective reversibility is the smaller of the two.
`requires_approval=True` pauses any node. Read-only investigation is allowed even
when situation risk is high. Hard blocks always win over approval.

```python
from daograph import Checkpoint, Context, DAOGraph, Node, Plan


def publish(ctx: Context):
    return {"receipt": "simulated"}  # Replace with an authorized external API.


graph = DAOGraph(
    goal="Publish an approved release",
    nodes=(Node("publish", publish, side_effect=True, reversibility=0.1),),
    planner=lambda ctx: Plan.chain("publish"),
)
paused = graph.invoke({})
assert paused.status == "paused"
saved = Checkpoint.from_json(paused.checkpoint.to_json())

# The host supplies this only after its own approval/authentication flow.
result = graph.resume(saved, approval=saved.interrupt.id)
assert result.status == "completed"
```

Writable state cannot grant approval or change the goal and constraints. On
resume, the runtime reassesses the situation, rechecks constraints, and runs the
exact pending task. A changed assessment invalidates the old approval if the
current decision still requires approval. A grant covers one task only.

Checkpoints and approval ids are trusted host data, not signed credentials. The
host must protect storage, authenticate approvers, serialize resumes, and retire
consumed checkpoints. Reusing the same saved checkpoint can repeat an external
effect. Use idempotency keys in effectful tools. Python callbacks run with host
permissions; DAOGraph is not a code sandbox. These boundaries are described in
[the architecture document](docs/architecture.md).

## 🔧 Streams, async calls, and state

```python
for event in graph.stream({}):
    print(event.kind, event.message)

# In an async application:
# result = await graph.ainvoke({})
# async for event in graph.astream({}):
#     ...
# result = await graph.aresume(saved, approval=saved.interrupt.id)
```

State must contain JSON-compatible data. Callbacks receive deeply read-only
snapshots; arrays appear as tuples. Partial updates replace top-level keys.
Optional `reducers={"messages": operator.add}` accumulates selected keys when a
previous value exists. A patch commits atomically after every reducer and value
validates. Node failures produce `Result(status="failed")` and are not retried.
Cancellation propagates to the caller. Cancelling an await does not forcibly stop
an already running synchronous callback thread.

Terminal statuses are `completed`, `paused`, `blocked`, and `failed`.
`completed` means the planner has no unfinished tasks; semantic goal verification
belongs in your planner or verification node. A paused run includes a serializable
approval checkpoint. Stream events expose state snapshots, plans, and situation
assessments, so stream consumers should receive only data they are authorized to
read.

## 📚 Relationship to LangGraph

LangGraph already supports conditional edges, dynamic routing through `Command`,
and durable interrupt/resume mechanisms.[^1][^2] DAOGraph's distinction is its
small default control contract: explicit situation assessment and reconstruction
of the transient execution DAG after every observation. It is an independent
implementation, with no LangGraph dependency and no API compatibility claim.

This alpha release includes DAG validation, sync/async calls, incremental event
streams, reducers, minimal constraints, and JSON approval checkpoints. Production
crash recovery, exactly-once effects, distributed scheduling, automatic retries,
intra-run parallelism, and a provider integration catalog are outside 0.1.0.

## 🤝 Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for the local quality checks and contribution
scope. New features should preserve the three-module core. DAOGraph is distributed
under the [MIT License](LICENSE).

[^1]: LangChain. [Graph API overview](https://docs.langchain.com/oss/python/langgraph/graph-api).
[^2]: LangChain. [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts).
