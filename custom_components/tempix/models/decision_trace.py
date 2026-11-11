"""Audit trail of one engine decision.

Every policy that intervenes appends a ``ConstraintTrace`` (input value,
output value, reason) — the final ``DecisionTrace`` is the gap-free
"reasoning chain" proving WHY the system decided the way it did.
Primitive values only.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Trace values are primitives: temperatures (float), modes (str) or None.
TraceValue = float | str | None


@dataclass(frozen=True)
class ConstraintTrace:
    """One policy intervention: what went in, what came out, and why."""

    policy: str
    input_value: TraceValue
    output_value: TraceValue
    reason: str


@dataclass(frozen=True)
class DecisionTrace:
    """Pure container — the ordered chain of all interventions."""

    entries: tuple[ConstraintTrace, ...] = field(default=())
