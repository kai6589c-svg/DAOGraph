"""Small, host-defined execution boundaries; no policy or role subsystem."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .situation import Situation

if TYPE_CHECKING:
    from .graph import Context, Node


@dataclass(frozen=True)
class Decision:
    kind: Literal["allow", "pause", "block"] = "allow"
    reason: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("allow", "pause", "block"):
            raise ValueError("decision must be allow, pause, or block")
        if not isinstance(self.reason, str):
            raise TypeError("reason must be a string")

    @classmethod
    def allow(cls) -> Decision:
        return cls()

    @classmethod
    def pause(cls, reason: str) -> Decision:
        return cls("pause", reason)

    @classmethod
    def block(cls, reason: str) -> Decision:
        return cls("block", reason)


Rule = Callable[["Context", "Node"], Decision]


@dataclass(frozen=True)
class Constraints:
    """Minimal boundaries, checked immediately before every action.

    Read-only nodes never require approval from situation scores alone. Nodes
    marked ``side_effect=True`` pause when risk or irreversibility exceeds the
    configured threshold. Explicit approval flags apply to any node. A hard
    block always wins over all pauses and approval grants.
    """

    max_steps: int = 32
    allowed_nodes: frozenset[str] | None = None
    approval_risk: float = 0.7
    approval_reversibility: float = 0.3
    rules: tuple[Rule, ...] = ()

    def __post_init__(self) -> None:
        if type(self.max_steps) is not int or self.max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        Situation(risk=self.approval_risk, reversibility=self.approval_reversibility)
        if self.allowed_nodes is not None:
            if isinstance(self.allowed_nodes, str) or any(
                not isinstance(name, str) or not name for name in self.allowed_nodes
            ):
                raise ValueError("allowed_nodes must contain nonempty names")
            object.__setattr__(self, "allowed_nodes", frozenset(self.allowed_nodes))
        if not all(callable(rule) for rule in self.rules):
            raise TypeError("rules must be callable")
        object.__setattr__(self, "rules", tuple(self.rules))

    def check(self, context: Context, node: Node) -> Decision:
        if context.steps >= self.max_steps:
            return Decision.block("Maximum executed steps reached")
        if self.allowed_nodes is not None and node.name not in self.allowed_nodes:
            return Decision.block(f"Node {node.name!r} is outside the allowlist")
        pauses = []
        if node.requires_approval:
            pauses.append("Node requires host approval")
        risk = max(context.situation.risk, node.risk)
        reversibility = min(context.situation.reversibility, node.reversibility)
        if node.side_effect and (
            risk >= self.approval_risk or reversibility <= self.approval_reversibility
        ):
            pauses.append("Side effect exceeds the risk or reversibility threshold")
        for rule in self.rules:
            decision = rule(context, node)
            if not isinstance(decision, Decision):
                raise TypeError("Constraint rules must return a Decision synchronously")
            if decision.kind == "block":
                return decision
            if decision.kind == "pause":
                pauses.append(decision.reason)
        return Decision.pause("; ".join(pauses)) if pauses else Decision.allow()
