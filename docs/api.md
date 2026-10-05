# DAOGraph API reference

_Public interfaces for the three-module core._

---

## 📋 Runtime configuration

```text
DAOGraph(
    goal: str,
    nodes: tuple[Node, ...],
    planner: Callable[[Context], Plan | Awaitable[Plan]],
    situation: SituationEngine = SituationEngine(),
    constraints: Constraints = Constraints(),
    reducers: Mapping[str, Callable] = {},
    revision: str = "1",
    adapt: Adapter | None = None,
    verify_goal: GoalVerifier | None = None,
)
```

Configuration is frozen. Node sequences and reducer mappings are copied during
construction. `revision` must change when callback logic or captured behavior
changes, so old approval checkpoints cannot silently resume under new semantics.

| Method | Returns |
| --- | --- |
| `invoke(state)` | `Result` |
| `stream(state)` | Incremental iterator of `Event` |
| `await ainvoke(state)` | `Result` |
| `astream(state)` | Async generator of `Event` |
| `resume(checkpoint, approval=id)` | `Result` |
| `await aresume(checkpoint, approval=id)` | `Result` |

Use the async variants inside an active event loop. Sync callbacks execute in
worker threads. Async callbacks are awaited; callback objects returning an
awaitable are also supported. Callback code must be thread-safe if independent
runs share it. Custom constraint rules must be synchronous; async policy checks
can be performed by trusted host code or explicit graph tasks.

Invalid construction, non-JSON initial state, and invalid resume arguments raise
exceptions. Callback, plan validation, reducer, and policy evaluation errors
during execution produce `Result(status="failed", reason="Type: message")`.
`asyncio.CancelledError` propagates instead of becoming a failed result.

## 🔧 Nodes and plans

`Node(name, action, side_effect=False, requires_approval=False, risk=0.0,
reversibility=1.0)` registers a capability. Names must be English identifiers.
Actions accept one `Context` and return a mapping of partial state updates.
Returning `None` or arbitrary objects fails the run.

`Step(id, node, after=())` describes one occurrence of a registered capability.
Task ids use English letters, digits, periods, underscores, colons, or hyphens.
`after` lists task ids that must have completed. A completed occurrence cannot be
redefined, even if a later plan includes it again.

`Plan(steps=(), reason="")` is a transient DAG. `Plan.chain("research", "answer")`
creates steps with ids equal to node names and sequential dependencies.
`Plan()` or a plan containing only completed ids ends a run when verification
is absent; with an unsatisfied verifier it returns `incomplete`.

```python
from daograph import Plan, Step

plan = Plan(
    (
        Step("search:1", "search"),
        Step("check:1", "verify", after=("search:1",)),
        Step("search:2", "search", after=("check:1",)),
    ),
    reason="Check the first result before searching again",
)
print(plan.to_mermaid())
```

`Plan.to_dict()` and `Plan.from_dict(data)` serialize plans.
`Plan.ready(completed_ids)` returns tasks whose dependencies are resolved.
`Plan.to_mermaid()` renders the current topology as Mermaid source; it does not
execute or validate the registered capabilities.

## 📊 Context and situation

`Context` exposes `goal`, `state`, `situation`, and `history`. `history` is a tuple
of successful `Step` occurrences. `steps` is its length; `completed` returns ids.
JSON arrays in `state` are immutable tuples, including nested arrays.

`Situation(risk=0.0, uncertainty=0.5, urgency=0.0, reversibility=1.0,
resource_pressure=0.0, trust=0.5, evidence=(), signals=())` validates finite scores in `[0, 1]`.
`labels` applies these convenience thresholds:

| Label | Threshold |
| --- | --- |
| `high_risk` | `risk >= 0.7` |
| `uncertain` | `uncertainty >= 0.6` |
| `time_critical` | `urgency >= 0.7` |
| `hard_to_reverse` | `reversibility <= 0.3` |
| `resource_constrained` | `resource_pressure >= 0.7` |
| `low_trust` | `trust <= 0.3` |
| `stable` | No other labels apply |

Labels are heuristics and do not guarantee safety or completion. They do not
change the constraints. `to_dict()` and `from_dict(data)` serialize a situation.

`SituationEngine(assess=...)` stores a callable with signature
`assess(state, previous) -> Situation | Awaitable[Situation]`. `previous` is `None`
before the first action. The default assessor reads `state["signals"]`; missing
fields use defaults rather than persisting previous values.

## 🧭 Adaptation and goal checks

`ReplanDecision(required: bool, reason: str)` and
`GoalCheck(satisfied: bool, reason: str)` are frozen validated records. Boolean
fields require actual booleans; reasons require strings.

- `adapt(context, previous_situation, current_plan)` returns a `ReplanDecision`,
  synchronously or asynchronously. It runs after successful tasks when unfinished
  work remains. `previous_situation` is the assessment before the last committed
  task; `current_plan` is the previously validated plan. Initial and exhausted
  plans force planning without calling the adapter. `None` plans every assessment.
- `verify_goal(context)` returns a `GoalCheck`, synchronously or asynchronously,
  after assessment and before selecting ordinary work. Satisfaction stops without
  dispatching pending tasks. An empty newly composed plan with an unsatisfied
  check returns `incomplete`. `None` retains original completion semantics.

Reused plans undergo validation and all selected actions pass constraints.
Invalid callback outputs return `failed` before dispatch. New optional fields
are appended after existing configuration fields, preserving positional arguments.

`Signal(name, value, evidence=())` is a frozen record with an English identifier,
a normalized finite number or `None`, and string evidence references. Situation
signal names must be unique. `Situation.to_dict()` includes named signals only
when present. `Situation.from_dict()` accepts old payloads without that field.
This is separate from the default assessor's `state["signals"]` mapping, which
contains the entire serialized situation.

```python
from daograph import Signal, Situation

observation = Situation(
    signals=(
        Signal("coverage", 0.5, ("source:one",)),
        Signal("freshness", None, ("No publication date available",)),
    )
)
assert Situation.from_dict(observation.to_dict()) == observation
```

## 🛡️ Constraints

```python
Constraints(
    max_steps=32,
    allowed_nodes=None,
    approval_risk=0.7,
    approval_reversibility=0.3,
    rules=(),
)
```

`None` permits any registered capability. An empty allowlist blocks all tasks.
Each rule accepts `(context, node)` and returns `Decision`. Rules can inspect
trusted external authority through their own host-provided closure. They should
be deterministic or consult an authoritative permission source.

```python
from daograph import Constraints, Decision


def restrict_publish(context, node):
    if node.name == "publish" and context.state.get("environment") != "staging":
        return Decision.block("Publishing is restricted to staging")
    return Decision.allow()


boundaries = Constraints(max_steps=8, rules=(restrict_publish,))
```

`Decision.pause(reason)` asks for host approval; `Decision.block(reason)` denies
execution. All pause requests are collected unless a hard block is encountered.
A grant never overrides a hard block.

## 💾 Results and approval checkpoints

`Result` contains `status`, a read-only `state`, `situation`, successful `history`,
last `plan`, `reason`, optional `checkpoint`, and optional `goal_check`. `completed` is a tuple of
successful task ids. A pause checkpoint contains an `Interrupt` with `id`, `step`,
and `reason`. The id identifies one pending approval request.

```python
from pathlib import Path
from daograph import Checkpoint

# Only persist checkpoints in storage controlled by the trusted host.
# Path("run.checkpoint.json").write_text(result.checkpoint.to_json(), encoding="utf-8")
# restored = Checkpoint.from_json(Path("run.checkpoint.json").read_text(encoding="utf-8"))
```

Resume executes the exact pending task before running adaptation or goal
verification again. The signature includes optional callback identities only
when present. With both absent, its construction remains identical to 0.1.0;
old default checkpoints remain compatible with the same host callbacks and revision.
Callback names are not code hashes: bump `revision` after behavior changes.

The JSON schema version is `1`. Data includes execution facts and evidence; avoid
placing credentials in state. Checkpoints are not signed. An approval id is not
an authentication token. Host code authenticates and authorizes the approver
before passing the id to `resume`. Checkpoints must be retired after consumption;
replaying the same checkpoint can replay its external effect.

## 🔄 Events and merge behavior

| Event kind | Meaning |
| --- | --- |
| `situation` | Assessment for current facts |
| `plan` | Validated transient plan |
| `node_start` | Action about to execute |
| `node_end` | Update committed |
| `completed` | Goal verified, or no unfinished tasks when no verifier is configured |
| `adaptation` | Required/reuse decision, including forced planning |
| `plan_reused` | Validated unfinished plan retained |
| `goal_check` | Host verifier result |
| `incomplete` | Newly composed plan exhausted while goal remains unsatisfied |
| `paused` | Host approval needed |
| `blocked` | Hard boundary denied execution |
| `failed` | Callback or validation error |

Each `Event` carries `context`, `plan`, optional `step`, `message`, and optional
terminal `result`. Optional typed `adaptation`, `goal_check`, and `plan_change`
fields carry control details. `PlanChange(added, removed, changed)` describes
sorted unfinished step ids on validated `plan` events; completed tasks are excluded.
Changing a completed task remains an error. Reuse has no change record.
The plan attached to a `situation` event is the previous plan;
a `plan` event carries the newly composed plan, and `plan_reused` retains it. Closing a stream
before consuming `node_start` does not run its task. Closing immediately after
receiving `node_start` also does not run it; advancing the iterator starts the
callback. State is committed before emitting `node_end`.

Updates merge at the top level. A configured reducer receives copies of the old
value and new value if the key already exists. For a missing key, its first value
is assigned directly. Reducers may be sync or async. There is no automatic deep
merge: a patch for `signals` replaces the entire signals object.
