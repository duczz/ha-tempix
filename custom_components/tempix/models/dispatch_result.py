"""Typed dispatch results — fallbacks without exceptions.

The dispatcher never raises for incompatible devices: a room set to
``dry`` sends ``dry`` to capable devices and the fallback ``off`` to the
rest, reported per device via ``DeviceDispatchResult``. ``FAILED``
entries feed ``RoomHealth``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from custom_components.tempix.models.hardware_command import HardwareCommand


class DeviceDispatchResult(Enum):
    SUCCESS = "success"
    # Device cannot do the requested mode; a safe fallback was sent.
    FALLBACK_APPLIED = "fallback_applied"
    # Service call failed (battery empty, Zigbee timeout, …).
    FAILED = "failed"
    # NOTHING was attempted for this device — the dispatcher suppressed the
    # planned command (repeat guard inside the TTL, circuit-breaker backoff,
    # min_delta). Carries the reason in ``detail`` and NEVER a ``command``.
    # Deliberately part of ``results`` although nothing went out: a suppressed
    # device used to vanish from the summary entirely, which made a room that
    # was silently not being controlled indistinguishable from one with nothing
    # to do. ``RoomHealth`` ignores these — health is
    # derived from ATTEMPTED dispatches only, so a skip neither
    # heals nor breaks it.
    SKIPPED = "skipped"


@dataclass(frozen=True)
class DeviceDispatch:
    """Outcome of dispatching one command to one device."""

    entity_id: str
    result: DeviceDispatchResult
    # e.g. the fallback mode that was applied, or a failure reason.
    detail: str | None = None
    # The command whose send was ATTEMPTED, after any capability rewrite
    # (dry → off). ``None`` means nothing left the dispatcher for this device:
    # unsupported mode with no ``off`` to fall back to, or dry-run. Callers that
    # must not act on phantom sends — echo bookkeeping above all — key on this,
    # not on ``result``: a FALLBACK_APPLIED can be either a real send or the
    # no-``off`` skip.
    #
    # Devices suppressed EARLIER (repeat guard, min_delta, circuit-breaker
    # backoff) DO appear — as ``SKIPPED`` with the reason in ``detail`` and
    # ``command=None``. They are still not sends, so
    # everything that asks "what went out" keys on ``command``, as before.
    command: HardwareCommand | None = None


@dataclass(frozen=True)
class DispatchSummary:
    """Container for all per-device outcomes of one dispatch cycle."""

    results: tuple[DeviceDispatch, ...] = field(default=())

    @property
    def all_ok(self) -> bool:
        return all(r.result is not DeviceDispatchResult.FAILED for r in self.results)

    @property
    def sent_commands(self) -> tuple[HardwareCommand, ...]:
        """Commands that actually went out, in dispatch order — the fallback
        rewrite applied. Empty for a cycle in which everything was suppressed."""
        return tuple(r.command for r in self.results if r.command is not None)

    @property
    def failed_entities(self) -> tuple[str, ...]:
        return tuple(
            r.entity_id for r in self.results
            if r.result is DeviceDispatchResult.FAILED
        )
