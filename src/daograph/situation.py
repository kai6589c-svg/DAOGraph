"""Situation assessment: explain facts without owning execution or authority."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields
from numbers import Real
from typing import Any


@dataclass(frozen=True)
class Situation:
    """Normalized signals. Scores are application estimates, not probabilities."""

    risk: float = 0.0
    uncertainty: float = 0.5
    urgency: float = 0.0
    reversibility: float = 1.0
    resource_pressure: float = 0.0
    trust: float = 0.5
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in fields(self):
            if field.name == "evidence":
                continue
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, Real):
                raise ValueError(f"{field.name} must be a finite number in [0, 1]")
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{field.name} must be a finite number in [0, 1]")
            object.__setattr__(self, field.name, float(value))
        if isinstance(self.evidence, str) or any(
            not isinstance(item, str) for item in self.evidence
        ):
            raise ValueError("evidence must be a sequence of strings")
        object.__setattr__(self, "evidence", tuple(self.evidence))

    @property
    def labels(self) -> tuple[str, ...]:
        """Convenience labels; thresholds are explicit heuristics."""
        values = (
            (self.risk >= 0.7, "high_risk"),
            (self.uncertainty >= 0.6, "uncertain"),
            (self.urgency >= 0.7, "time_critical"),
            (self.reversibility <= 0.3, "hard_to_reverse"),
            (self.resource_pressure >= 0.7, "resource_constrained"),
            (self.trust <= 0.3, "low_trust"),
        )
        return tuple(label for active, label in values if active) or ("stable",)

    def to_dict(self) -> dict[str, Any]:
        return {
            field.name: list(self.evidence)
            if field.name == "evidence"
            else getattr(self, field.name)
            for field in fields(self)
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Situation:
        return cls(**dict(value))


Assessor = Callable[[Mapping[str, Any], Situation | None], Situation | Awaitable[Situation]]


def _read_signals(state: Mapping[str, Any], previous: Situation | None) -> Situation:
    """Read explicitly supplied signals; never infer risk from arbitrary prose."""
    signals = state.get("signals", {})
    if not isinstance(signals, Mapping):
        raise ValueError("state['signals'] must be a mapping")
    return Situation.from_dict(signals)


@dataclass(frozen=True)
class SituationEngine:
    """Replace the assessor with domain logic or a model-backed async function.

    The default reads the latest complete ``state['signals']`` mapping. Missing
    fields use Situation defaults; values do not silently carry over from history.
    """

    assess: Assessor = _read_signals

    def __post_init__(self) -> None:
        if not callable(self.assess):
            raise TypeError("assess must be callable")
