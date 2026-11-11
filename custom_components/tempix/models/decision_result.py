"""DecisionResult — the final engine output aggregate.

Bundles everything one evaluation produced. ``snapshot_id`` and
``validity`` are carried through for hardware safeguards (UNSAFE) and
diagnostics correlation. ``hardware_commands`` is a tuple (the immutable
equivalent of a list, required by frozen=True).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from custom_components.tempix.models.calibration_command import CalibrationCommand
from custom_components.tempix.models.decision_trace import DecisionTrace
from custom_components.tempix.models.hardware_command import HardwareCommand
from custom_components.tempix.models.priorities import GoalClass
from custom_components.tempix.models.snapshot import SnapshotValidity
from custom_components.tempix.models.target_state import TargetState
from custom_components.tempix.models.valve_command import ValveCommand


@dataclass(frozen=True)
class DecisionResult:
    """Immutable result of exactly one engine evaluation."""

    target_state: TargetState
    # Which goal won the selection (status classification, V1/V2 diffs).
    goal_class: GoalClass | None = None
    # One command per managed device (multi-device rooms → plural).
    hardware_commands: tuple[HardwareCommand, ...] = field(default=())
    # Valve positioning and entity-based calibration
    # are SEPARATE command kinds — a device can carry a climate
    # command AND a valve position AND a calibration write in the same cycle.
    # Planned in parallel validation, sent post-flip by the dispatcher.
    valve_commands: tuple[ValveCommand, ...] = field(default=())
    calibration_commands: tuple[CalibrationCommand, ...] = field(default=())
    trace: DecisionTrace = field(default_factory=DecisionTrace)
    snapshot_id: str = ""
    validity: SnapshotValidity = SnapshotValidity.VALID
    # Klartext-Begruendung, warum die Validity nicht VALID ist — reicht die
    # Snapshot-Gruende bis in den Diagnose-Export durch. Der Coordinator haelt
    # nur das Result, nicht den Snapshot; ohne dieses Feld endet die Spur bei
    # einem nackten "degraded".
    validity_reasons: tuple[str, ...] = field(default=())
    # Write-back isolation: the engine never mutates override state — it
    # only NAMES the override that expired; nothing consumes it yet
    # (diagnostics only), V1 owns the override lifecycle.
    expired_override_id: str | None = None
    # Earliest moment the decision would change WITHOUT a new event
    # (override TTL, next slot/calendar transition, party timer end,
    # preconditioning start). The coordinator arms its V2 wakeup timer
    # (``_schedule_v2_wakeup``) from this — min() over all policies'
    # next_boundary().
    next_evaluation_at: datetime | None = None
