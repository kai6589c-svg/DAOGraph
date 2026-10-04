"""A small adaptive DAG runtime with streaming and approval checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
import html
import inspect
import json
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, TypeVar, cast

from .constraints import Constraints
from .situation import Situation, SituationEngine

T = TypeVar("T")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_STEP_ID = re.compile(r"[A-Za-z0-9_.:-]+\Z")


def _plain(value: Any) -> Any:
    """Copy JSON data and reject objects that cannot survive a checkpoint."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not (-float("inf") < value < float("inf")):
            raise ValueError("State numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("State mapping keys must be strings")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    raise TypeError(f"State must contain JSON data, got {type(value).__name__}")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _snapshot(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("State and updates must be mappings")
    return cast(Mapping[str, Any], _freeze(_plain(value)))


def _call_name(fn: Callable[..., Any]) -> str:
    return f"{getattr(fn, '__module__', '')}.{getattr(fn, '__qualname__', type(fn).__name__)}"


async def _call(fn: Callable[..., T | Awaitable[T]], *args: Any) -> T:
    # Sync callbacks run in a worker so they do not block the async event loop.
    value = (
        await fn(*args) if inspect.iscoroutinefunction(fn) else await asyncio.to_thread(fn, *args)
    )
    return await value if inspect.isawaitable(value) else value


async def _advance(iterator: AsyncIterator[T]) -> T:
    return await anext(iterator)


@dataclass(frozen=True)
class Step:
    """A task occurrence. Reuse a node with a fresh id to execute it again."""

    id: str
    node: str
    after: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _STEP_ID.fullmatch(self.id):
            raise ValueError("Step ids must use English letters, digits, . _ : or -")
        if not isinstance(self.node, str) or not _NAME.fullmatch(self.node):
            raise ValueError("Node names must be English identifiers")
        if isinstance(self.after, str) or any(
            not isinstance(item, str) or not _STEP_ID.fullmatch(item) for item in self.after
        ):
            raise ValueError("after must be a sequence of step ids")
        object.__setattr__(self, "after", tuple(self.after))
        if len(set(self.after)) != len(self.after) or self.id in self.after:
            raise ValueError("Dependencies must be unique and cannot include the step itself")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "node": self.node, "after": list(self.after)}


@dataclass(frozen=True)
class Plan:
    """A transient DAG. Each assessment can replace all unfinished tasks."""

    steps: tuple[Step, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))
        if not all(isinstance(step, Step) for step in self.steps):
            raise TypeError("Plan steps must be Step objects")
        if not isinstance(self.reason, str):
            raise TypeError("Plan reason must be a string")
        ids = {step.id for step in self.steps}
        if len(ids) != len(self.steps):
            raise ValueError("Duplicate step id in plan")
        # External dependencies are resolved against execution history at runtime.
        remaining = {step.id: set(step.after) & ids for step in self.steps}
        while remaining:
            roots = {key for key, deps in remaining.items() if not deps}
            if not roots:
                raise ValueError("Plan contains a dependency cycle")
            remaining = {key: deps - roots for key, deps in remaining.items() if key not in roots}

    @classmethod
    def chain(cls, *nodes: str, reason: str = "") -> Plan:
        return cls(
            tuple(
                Step(name, name, (nodes[index - 1],) if index else ())
                for index, name in enumerate(nodes)
            ),
            reason,
        )

    def validate(self, nodes: Mapping[str, Node], history: tuple[Step, ...]) -> None:
        completed = {step.id: step for step in history}
        ids = {step.id for step in self.steps} | completed.keys()
        for step in self.steps:
            if step.node not in nodes:
                raise ValueError(f"Unknown node {step.node!r}")
            if any(dep not in ids for dep in step.after):
                raise ValueError(f"Unknown dependency for step {step.id!r}")
            if step.id in completed and step != completed[step.id]:
                raise ValueError(f"Completed step {step.id!r} cannot be redefined")

    def ready(self, completed: tuple[str, ...]) -> tuple[Step, ...]:
        done = set(completed)
        return tuple(step for step in self.steps if step.id not in done and set(step.after) <= done)

    def to_dict(self) -> dict[str, Any]:
        return {"steps": [step.to_dict() for step in self.steps], "reason": self.reason}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Plan:
        return cls(tuple(Step(**step) for step in value["steps"]), value["reason"])

    def to_mermaid(self) -> str:
        lines = [
            "flowchart TD",
            "    accTitle: Current adaptive execution plan",
            "    accDescr: Tasks and dependencies for one situation assessment.",
        ]
        ids = {step.id: f"task_{index}" for index, step in enumerate(self.steps)}
        dependencies = sorted({dep for step in self.steps for dep in step.after} - ids.keys())
        ids.update({dep: f"completed_{index}" for index, dep in enumerate(dependencies)})
        for dep in dependencies:
            lines.append(f'    {ids[dep]}["{html.escape(dep)} (completed)"]')
        for step in self.steps:
            label = html.escape(f"{step.id}: {step.node}")
            lines.append(f'    {ids[step.id]}["{label}"]')
            lines.extend(f"    {ids[dep]} --> {ids[step.id]}" for dep in step.after)
        if not self.steps:
            lines.append('    finished(["No remaining tasks"])')
        return "\n".join(lines)


@dataclass(frozen=True)
class Context:
    """Read-only inputs. Goal and authority never live in writable state."""

    goal: str
    state: Mapping[str, Any]
    situation: Situation
    history: tuple[Step, ...] = ()

    @property
    def steps(self) -> int:
        return len(self.history)

    @property
    def completed(self) -> tuple[str, ...]:
        return tuple(step.id for step in self.history)


Action = Callable[[Context], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]
Planner = Callable[[Context], Plan | Awaitable[Plan]]
Reducer = Callable[[Any, Any], Any]


@dataclass(frozen=True)
class Node:
    name: str
    action: Action
    side_effect: bool = False
    requires_approval: bool = False
    risk: float = 0.0
    reversibility: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise ValueError("Node names must be English identifiers")
        if not callable(self.action):
            raise TypeError("Node action must be callable")
        if type(self.side_effect) is not bool or type(self.requires_approval) is not bool:
            raise TypeError("Node flags must be booleans")
        Situation(risk=self.risk, reversibility=self.reversibility)


@dataclass(frozen=True)
class Interrupt:
    id: str
    step: Step
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "step": self.step.to_dict(), "reason": self.reason}


@dataclass(frozen=True)
class Checkpoint:
    """An approval checkpoint. Persist in host-controlled storage."""

    graph_signature: str
    state: Mapping[str, Any]
    situation: Situation
    history: tuple[Step, ...]
    plan: Plan
    interrupt: Interrupt

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _snapshot(self.state))
        object.__setattr__(self, "history", tuple(self.history))

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": 1,
                "graph_signature": self.graph_signature,
                "state": _plain(self.state),
                "situation": self.situation.to_dict(),
                "history": [step.to_dict() for step in self.history],
                "plan": self.plan.to_dict(),
                "interrupt": self.interrupt.to_dict(),
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, value: str) -> Checkpoint:
        data = json.loads(value)
        if data.pop("schema") != 1:
            raise ValueError("Unsupported checkpoint schema")
        data["situation"] = Situation.from_dict(data["situation"])
        data["history"] = tuple(Step(**step) for step in data["history"])
        data["plan"] = Plan.from_dict(data["plan"])
        interrupt = data["interrupt"]
        data["interrupt"] = Interrupt(
            interrupt["id"], Step(**interrupt["step"]), interrupt["reason"]
        )
        return cls(**data)


@dataclass(frozen=True)
class Result:
    status: Literal["completed", "paused", "blocked", "failed"]
    state: Mapping[str, Any]
    situation: Situation
    history: tuple[Step, ...]
    plan: Plan
    reason: str = ""
    checkpoint: Checkpoint | None = None

    @property
    def completed(self) -> tuple[str, ...]:
        return tuple(step.id for step in self.history)


@dataclass(frozen=True)
class Event:
    kind: str
    context: Context
    plan: Plan
    step: Step | None = None
    message: str = ""
    result: Result | None = None


@dataclass(frozen=True)
class DAOGraph:
    """Declare capabilities and a planner; recompute the graph after every action.

    Callbacks and checkpoints are trusted host code/data. This is orchestration,
    not a Python sandbox or a distributed exactly-once execution service.
    """

    goal: str
    nodes: tuple[Node, ...]
    planner: Planner
    situation: SituationEngine = field(default_factory=SituationEngine)
    constraints: Constraints = field(default_factory=Constraints)
    reducers: Mapping[str, Reducer] = field(default_factory=dict)
    revision: str = "1"
    _registry: Mapping[str, Node] = field(init=False, repr=False)
    _signature: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValueError("goal must be a nonempty string")
        if not isinstance(self.revision, str) or not self.revision:
            raise ValueError("revision must be a nonempty string")
        object.__setattr__(self, "nodes", tuple(self.nodes))
        if not all(isinstance(node, Node) for node in self.nodes):
            raise TypeError("nodes must contain Node objects")
        registry = {node.name: node for node in self.nodes}
        if len(registry) != len(self.nodes):
            raise ValueError("Duplicate registered node name")
        if not callable(self.planner):
            raise TypeError("planner must be callable")
        if not isinstance(self.constraints, Constraints):
            raise TypeError("constraints must be Constraints")
        if not isinstance(self.situation, SituationEngine):
            raise TypeError("situation must be SituationEngine")
        if not all(isinstance(key, str) and callable(fn) for key, fn in self.reducers.items()):
            raise TypeError("reducers must map state keys to callable reducers")
        object.__setattr__(self, "reducers", MappingProxyType(dict(self.reducers)))
        object.__setattr__(self, "_registry", MappingProxyType(registry))
        definition = {
            "goal": self.goal,
            "revision": self.revision,
            "nodes": [
                [
                    n.name,
                    _call_name(n.action),
                    n.side_effect,
                    n.requires_approval,
                    n.risk,
                    n.reversibility,
                ]
                for n in self.nodes
            ],
            "planner": _call_name(self.planner),
            "assessor": _call_name(self.situation.assess),
            "constraints": [
                self.constraints.max_steps,
                sorted(self.constraints.allowed_nodes)
                if self.constraints.allowed_nodes is not None
                else None,
                self.constraints.approval_risk,
                self.constraints.approval_reversibility,
                [_call_name(rule) for rule in self.constraints.rules],
            ],
            "reducers": {key: _call_name(fn) for key, fn in self.reducers.items()},
        }
        signature = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
        object.__setattr__(self, "_signature", signature)

    async def _drive(
        self,
        state: Mapping[str, Any],
        checkpoint: Checkpoint | None = None,
        approval: str | None = None,
    ) -> AsyncGenerator[Event, None]:
        facts = _plain(_snapshot(state))
        history = checkpoint.history if checkpoint else ()
        current = checkpoint.situation if checkpoint else Situation()
        plan = checkpoint.plan if checkpoint else Plan()
        pending = checkpoint.interrupt.step if checkpoint else None
        previous = checkpoint.situation if checkpoint else None
        try:
            while True:
                snapshot = _snapshot(facts)
                current = await _call(self.situation.assess, snapshot, previous)
                if not isinstance(current, Situation):
                    raise TypeError("Assessor must return a Situation")
                context = Context(self.goal, snapshot, current, history)
                yield Event("situation", context, plan)
                if pending is None:
                    plan = await _call(self.planner, context)
                    if not isinstance(plan, Plan):
                        raise TypeError("Planner must return a Plan")
                plan.validate(self._registry, history)
                yield Event("plan", context, plan, message=plan.reason)
                ready = plan.ready(context.completed)
                if pending is not None:
                    if pending not in ready:
                        raise ValueError("Checkpoint pending step is not ready")
                    step = pending
                elif ready:
                    step = ready[0]
                else:
                    if any(step.id not in context.completed for step in plan.steps):
                        raise ValueError("Plan has unfinished tasks but no ready task")
                    result = Result("completed", snapshot, current, history, plan, plan.reason)
                    yield Event("completed", context, plan, result=result)
                    return
                node = self._registry[step.node]
                decision = await _call(self.constraints.check, context, node)
                if decision.kind == "block":
                    result = Result("blocked", snapshot, current, history, plan, decision.reason)
                    yield Event("blocked", context, plan, step, decision.reason, result)
                    return
                granted = (
                    checkpoint is not None
                    and pending == step
                    and approval == checkpoint.interrupt.id
                    and current == checkpoint.situation
                )
                if decision.kind == "pause" and not granted:
                    interrupt = Interrupt(uuid.uuid4().hex, step, decision.reason)
                    saved = Checkpoint(self._signature, snapshot, current, history, plan, interrupt)
                    result = Result(
                        "paused", snapshot, current, history, plan, decision.reason, saved
                    )
                    yield Event("paused", context, plan, step, decision.reason, result)
                    return
                yield Event("node_start", context, plan, step)
                patch = await _call(node.action, context)
                patch = _plain(_snapshot(patch))
                # Commit atomically only after every reducer and value validates.
                updated = _plain(facts)
                for key, value in patch.items():
                    reducer = self.reducers.get(key)
                    updated[key] = (
                        _plain(await _call(reducer, _plain(facts[key]), value))
                        if reducer is not None and key in facts
                        else value
                    )
                facts = updated
                history = (*history, step)
                after = Context(self.goal, _snapshot(facts), current, history)
                yield Event("node_end", after, plan, step)
                previous = current
                pending = None
                checkpoint = None
                approval = None
        except Exception as error:
            context = Context(self.goal, _snapshot(facts), current, history)
            reason = f"{type(error).__name__}: {error}"
            result = Result("failed", context.state, current, history, plan, reason)
            yield Event("failed", context, plan, message=reason, result=result)

    async def astream(self, state: Mapping[str, Any]) -> AsyncGenerator[Event, None]:
        async for event in self._drive(state):
            yield event

    async def ainvoke(self, state: Mapping[str, Any]) -> Result:
        async for event in self.astream(state):
            if event.result is not None:
                return event.result
        raise RuntimeError("Run ended without a result")

    def stream(self, state: Mapping[str, Any]) -> Iterator[Event]:
        self._require_sync()
        with asyncio.Runner() as runner:
            iterator = self.astream(state)
            try:
                while True:
                    try:
                        yield runner.run(_advance(iterator))
                    except StopAsyncIteration:
                        return
            finally:
                runner.run(iterator.aclose())

    def invoke(self, state: Mapping[str, Any]) -> Result:
        self._require_sync()
        return asyncio.run(self.ainvoke(state))

    def _validate_resume(self, checkpoint: Checkpoint, approval: str) -> None:
        if not isinstance(checkpoint, Checkpoint):
            raise TypeError("checkpoint must be a Checkpoint")
        if checkpoint.graph_signature != self._signature:
            raise ValueError("Checkpoint belongs to a different graph definition or revision")
        if not isinstance(approval, str) or approval != checkpoint.interrupt.id:
            raise ValueError("Approval must match the exact checkpoint interrupt id")
        if len({step.id for step in checkpoint.history}) != len(checkpoint.history):
            raise ValueError("Checkpoint contains duplicate completed steps")
        for step in checkpoint.history:
            if step.node not in self._registry:
                raise ValueError("Checkpoint history contains an unknown node")
        checkpoint.plan.validate(self._registry, checkpoint.history)
        if checkpoint.interrupt.step not in checkpoint.plan.ready(
            tuple(step.id for step in checkpoint.history)
        ):
            raise ValueError("Checkpoint interrupt is not a ready task")

    async def aresume(self, checkpoint: Checkpoint, *, approval: str) -> Result:
        """Recheck constraints and execute the exact host-approved pending task.

        A changed assessment invalidates the old grant and creates a new pause.
        The application must serialize access and retire consumed checkpoints.
        """
        self._validate_resume(checkpoint, approval)
        async for event in self._drive(checkpoint.state, checkpoint, approval):
            if event.result is not None:
                return event.result
        raise RuntimeError("Resume ended without a result")

    def resume(self, checkpoint: Checkpoint, *, approval: str) -> Result:
        self._require_sync()
        return asyncio.run(self.aresume(checkpoint, approval=approval))

    @staticmethod
    def _require_sync() -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise RuntimeError("Use the async API inside an active event loop")
