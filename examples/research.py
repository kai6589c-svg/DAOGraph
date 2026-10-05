"""Bounded evidence-driven research application, independent of model providers.

The host supplies requirements and source provenance. Documents supply claims,
never authority, budgets, expected answers, or execution permissions.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from daograph import (
    Constraints,
    Context,
    DAOGraph,
    GoalCheck,
    Node,
    Plan,
    ReplanDecision,
    Signal,
    Situation,
    SituationEngine,
    Step,
)


def plain(value):
    """Copy the runtime's deeply read-only JSON view for application updates."""
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


@dataclass(frozen=True)
class Task:
    question: str
    required_keys: tuple[str, ...]
    effective_date: str | None = None

    def __post_init__(self):
        if isinstance(self.required_keys, str):
            raise ValueError("Required keys must be a sequence, not a string")
        object.__setattr__(self, "required_keys", tuple(self.required_keys))
        if self.effective_date is not None:
            date.fromisoformat(self.effective_date)
        if (
            not isinstance(self.question, str)
            or not self.question
            or not self.required_keys
            or any(not isinstance(key, str) or not key for key in self.required_keys)
            or len(set(self.required_keys)) != len(self.required_keys)
        ):
            raise ValueError("Task needs a question and unique required keys")


@dataclass(frozen=True)
class Source:
    id: str
    origin: str
    url: str
    title: str
    date: str
    authoritative: bool = False
    duplicate_of: str | None = None

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", self.id):
            raise ValueError("Source id must be a safe English identifier")
        if not all(
            isinstance(value, str) and value
            for value in (self.origin, self.url, self.title, self.date)
        ):
            raise ValueError("Source provenance fields must be nonempty strings")
        date.fromisoformat(self.date)
        if type(self.authoritative) is not bool:
            raise ValueError("Source authority must be a host-defined boolean")


def rank_sources(question, sources):
    """Stable token overlap on host source titles, never document contents."""
    tokens = set(question.lower().split())
    return tuple(sorted(sources, key=lambda s: (-len(tokens & set(s.title.lower().split())), s.id)))


class Research:
    """Reference policy; extraction/reads are supplied by the host application."""

    def __init__(
        self, task: Task, sources: tuple[Source, ...], read, *, budget=4, fixed_depth=None
    ):
        if not 1 <= budget <= 4 or (fixed_depth is not None and not 1 <= fixed_depth <= budget):
            raise ValueError("Use a retrieval budget in [1, 4] and a bounded fixed depth")
        if type(budget) is not int or (fixed_depth is not None and type(fixed_depth) is not int):
            raise ValueError("Retrieval limits must be integers")
        if len({source.id for source in sources}) != len(sources):
            raise ValueError("Source ids must be unique")
        if any(
            source.duplicate_of is not None
            and (
                source.duplicate_of == source.id
                or source.duplicate_of not in {s.id for s in sources}
            )
            for source in sources
        ):
            raise ValueError("Known copies must refer to another configured source")
        self.task = task
        self.sources = rank_sources(task.question, sources)
        self.read = read
        self.budget = budget
        self.fixed_depth = fixed_depth
        self.planner_calls = 0

    def initial_state(self):
        return {"evidence": [], "attempts": [], "relevant_change": False}

    def _current(self, source):
        return self.task.effective_date is None or source["date"] >= self.task.effective_date

    def resolution(self, state):
        """Resolve structured claims using host provenance and current evidence."""
        resolved, conflicts, covered, fresh = {}, [], [], []
        for key in self.task.required_keys:
            observations = [
                (entry["source"], claim)
                for entry in state.get("evidence", ())
                for claim in entry["claims"]
                if claim["key"] == key
            ]
            if observations:
                covered.append(key)
            current = [(s, c) for s, c in observations if self._current(s)]
            if current:
                fresh.append(key)
            authoritative = [(s, c) for s, c in current if s["authoritative"]]
            relevant = authoritative or current
            values = {c["value"] for _, c in relevant}
            if len(values) > 1:
                conflicts.append(key)
            elif len(values) == 1 and (
                authoritative or len({s["origin"] for s, _ in relevant}) >= 2
            ):
                value = next(iter(values))
                resolved[key] = {
                    "value": value,
                    "citations": [
                        {"source_id": s["id"], "url": s["url"], "excerpt": c["excerpt"]}
                        for s, c in relevant
                    ],
                }
        return {"resolved": resolved, "conflicts": conflicts, "covered": covered, "fresh": fresh}

    def _fingerprint(self, state):
        # Copies sharing provenance and normalized values add no independent support.
        return {
            (
                c["key"],
                c["value"],
                e["source"]["origin"],
                self._current(e["source"]),
                e["source"]["authoritative"],
            )
            for e in state.get("evidence", ())
            for c in e["claims"]
            if c["key"] in self.task.required_keys
        }

    def remaining(self, state):
        attempted = {attempt["source_id"] for attempt in state.get("attempts", ())}
        return tuple(
            source
            for source in self.sources
            if source.id not in attempted and source.duplicate_of is None
        )

    def stopping(self, state):
        return len(state.get("attempts", ())) >= self.budget or not self.remaining(state)

    def assess(self, state, previous):
        result = self.resolution(state)
        n = len(self.task.required_keys)
        refs = tuple(entry["source"]["id"] for entry in state.get("evidence", ()))
        coverage = len(result["covered"]) / n
        support = len(result["resolved"]) / n
        conflict = len(result["conflicts"]) / max(1, len(result["covered"]))
        freshness = len(result["fresh"]) / n if self.task.effective_date else None
        return Situation(
            uncertainty=max(
                1 - coverage, 1 - support, conflict, 1 - freshness if freshness is not None else 0
            ),
            resource_pressure=len(state.get("attempts", ())) / self.budget,
            evidence=tuple(f"Observed source {ref}" for ref in refs),
            signals=tuple(
                Signal(name, value, refs)
                for name, value in (
                    ("coverage", coverage),
                    ("conflict", conflict),
                    ("independent_support", support),
                    ("freshness", freshness),
                    ("new_information", float(state.get("relevant_change", False))),
                )
            ),
        )

    def adapt(self, context, previous, plan):
        changed = bool(context.state.get("relevant_change"))
        # Verification changes answer state, not source evidence.
        if context.history and context.history[-1].node != "retrieve":
            changed = False
        return ReplanDecision(
            changed,
            "Relevant evidence or read failure"
            if changed
            else "Evidence unchanged; retain unfinished tasks",
        )

    def retrieve(self, context):
        state = plain(context.state)
        candidates = self.remaining(state)
        if not candidates or len(state["attempts"]) >= self.budget:
            raise RuntimeError("Planner exceeded the retrieval budget")
        source = candidates[0]
        attempt = {
            "query": self.task.question,
            "source_id": source.id,
            "outcome": "ok",
            "evidence_ids": [],
        }
        before = self._fingerprint(state)
        try:
            document = self.read(source)
        except (TimeoutError, FileNotFoundError, ConnectionError, OSError) as error:
            attempt["outcome"] = type(error).__name__
            document = None
        if document is None and attempt["outcome"] == "ok":
            attempt["outcome"] = "empty"
        if document is not None:
            if not isinstance(document, Mapping) or "claims" not in document:
                raise TypeError("Extractor must return a claims mapping")
            claims = document["claims"]
            if not isinstance(claims, (list, tuple)):
                raise TypeError("Claims must be a sequence")
            for claim in claims:
                if not isinstance(claim, Mapping) or not all(
                    isinstance(claim.get(k), str) and claim[k].strip()
                    for k in ("key", "value", "excerpt")
                ):
                    raise TypeError("Claims need nonempty key, value, and excerpt strings")
            if claims:
                state["evidence"].append({"source": asdict(source), "claims": plain(claims)})
                attempt["evidence_ids"] = [source.id]
            else:
                attempt["outcome"] = "empty"
        state["attempts"].append(attempt)
        state["relevant_change"] = before != self._fingerprint(state) or attempt["outcome"] != "ok"
        return state

    def plan(self, context: Context):
        self.planner_calls += 1
        if context.state.get("answer"):
            return Plan(reason="Research outcome delivered")
        completed = set(context.completed)
        count = len(context.state.get("attempts", ()))
        resolution = self.resolution(context.state)
        sufficient = len(resolution["resolved"]) == len(self.task.required_keys)
        limit = self.fixed_depth if self.fixed_depth is not None else self.budget
        fetch = min(limit - count, len(self.remaining(context.state)))
        if (self.fixed_depth is None and sufficient) or "verify" in completed:
            fetch = 0
        previous = context.history[-1].id if context.history else None
        steps = []
        if resolution["conflicts"] and "review" not in completed:
            steps.append(Step("review", "review", (previous,) if previous else ()))
            previous = "review"
        for number in range(count, count + max(0, fetch)):
            ident = f"retrieve:{number}"
            steps.append(Step(ident, "retrieve", (previous,) if previous else ()))
            previous = ident
        # Separate claim resolution from citation integrity; both are meaningful checks.
        for ident in ("verify", "citations", "answer"):
            if ident not in completed:
                steps.append(Step(ident, ident, (previous,) if previous else ()))
            previous = ident
        reason = (
            "Resolve conflict before answering"
            if resolution["conflicts"]
            else (
                "Evidence sufficient; verify and answer"
                if sufficient
                else "Investigate missing support"
            )
        )
        return Plan(tuple(steps), reason)

    def review(self, context):
        """Expose competing observed values before investigating another source."""
        conflicts = self.resolution(context.state)["conflicts"]
        alternatives = {
            key: sorted(
                {
                    claim["value"]
                    for entry in context.state["evidence"]
                    for claim in entry["claims"]
                    if claim["key"] == key
                }
            )
            for key in conflicts
        }
        return {"conflict_review": alternatives}

    def verify(self, context):
        result = self.resolution(context.state)
        unresolved = [key for key in self.task.required_keys if key not in result["resolved"]]
        return {
            "candidate": {
                "status": "insufficient" if unresolved else "supported",
                "claims": result["resolved"],
                "unresolved": unresolved,
            }
        }

    def check_citations(self, context):
        candidate = context.state["candidate"]
        observed = {
            (e["source"]["id"], c["key"], c["value"], c["excerpt"])
            for e in context.state["evidence"]
            for c in e["claims"]
        }
        valid = all(
            (citation["source_id"], key, claim["value"], citation["excerpt"]) in observed
            for key, claim in candidate["claims"].items()
            for citation in claim["citations"]
        )
        if not valid:
            raise ValueError("Citation does not match retrieved evidence")
        return {"citations_valid": True}

    def answer(self, context):
        if not context.state.get("citations_valid"):
            raise ValueError("Verify citations before answering")
        return {"answer": plain(context.state["candidate"])}

    def verify_goal(self, context):
        answer = context.state.get("answer")
        if not answer:
            return GoalCheck(False, "No verified answer yet")
        expected = self.verify(context)["candidate"]
        if plain(answer) != expected or not context.state.get("citations_valid"):
            return GoalCheck(False, "Answer does not match observed evidence")
        if answer["status"] == "insufficient" and not self.stopping(context.state):
            return GoalCheck(False, "Unresolved claims still have available retrieval budget")
        return GoalCheck(
            True,
            "Supported answer"
            if answer["status"] == "supported"
            else "Justified abstention: evidence or retrieval budget exhausted",
        )

    def graph(self, mode="selective"):
        if mode not in ("fixed", "always", "selective"):
            raise ValueError("Unknown research control mode")
        return DAOGraph(
            self.task.question,
            (
                Node("retrieve", self.retrieve),
                Node("review", self.review),
                Node("verify", self.verify),
                Node("citations", self.check_citations),
                Node("answer", self.answer),
            ),
            self.plan,
            situation=SituationEngine(self.assess),
            constraints=Constraints(max_steps=8),
            revision="research-0.2",
            adapt=(lambda *args: ReplanDecision(False, "Fixed workflow"))
            if mode == "fixed"
            else self.adapt
            if mode == "selective"
            else None,
            verify_goal=self.verify_goal,
        )


def trace_record(event):
    """JSON data suitable for a replayable, inspectable application trace."""
    record: dict[str, Any] = {
        "kind": event.kind,
        "completed": list(event.context.completed),
        "situation": event.context.situation.to_dict(),
        "message": event.message,
    }
    if event.kind == "plan":
        record["plan"] = event.plan.to_dict()
    if event.plan_change is not None:
        record["plan_change"] = asdict(event.plan_change)
    if event.adaptation is not None:
        record["adaptation"] = asdict(event.adaptation)
    if event.goal_check is not None:
        record["goal_check"] = asdict(event.goal_check)
    if event.step is not None:
        record["step"] = event.step.to_dict()
    if event.kind == "node_end" and event.step.node == "retrieve":
        record["observation"] = plain(event.context.state["attempts"][-1])
    if event.result is not None:
        record["status"] = event.result.status
        record["answer"] = plain(event.result.state.get("answer"))
    return record


def write_trace(events, path):
    path.write_text(
        "".join(json.dumps(trace_record(event), sort_keys=True) + "\n" for event in events)
    )
