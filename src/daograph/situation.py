"""Situation assessment: explain facts without owning execution or authority."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields
from numbers import Real
from typing import Any


@dataclass(frozen=True)
class Signal:
    """An application-defined normalized observation; None means unknown."""

    name: str
    value: float | None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not self.name.isascii()
            or not self.name.isidentifier()
        ):
            raise ValueError("Signal name must be an English identifier")
        if self.value is not None:
            if (
                isinstance(self.value, bool)
                or not isinstance(self.value, Real)
                or not math.isfinite(self.value)
                or not 0 <= self.value <= 1
            ):
                raise ValueError("Signal value must be None or a finite number in [0, 1]")
            object.__setattr__(self, "value", float(self.value))
        if isinstance(self.evidence, str) or any(not isinstance(x, str) for x in self.evidence):
            raise ValueError("Signal evidence must be a sequence of strings")
        object.__setattr__(self, "evidence", tuple(self.evidence))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "evidence": list(self.evidence)}


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
    signals: tuple[Signal, ...] = ()

    def __post_init__(self) -> None:
        for field in fields(self):
            if field.name in ("evidence", "signals"):
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

        object.__setattr__(self, "signals", tuple(self.signals))
        if not all(isinstance(signal, Signal) for signal in self.signals):
            raise TypeError("signals must contain Signal records")
        if len({signal.name for signal in self.signals}) != len(self.signals):
            raise ValueError("Signal names must be unique")

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
        data = {
            field.name: getattr(self, field.name)
            for field in fields(self)
            if field.name not in ("evidence", "signals")
        }
        data["evidence"] = list(self.evidence)
        if self.signals:
            data["signals"] = [signal.to_dict() for signal in self.signals]
        return data

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Situation:
        data = dict(value)
        if "signals" in data:
            data["signals"] = tuple(Signal(**signal) for signal in data["signals"])
        return cls(**data)


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
