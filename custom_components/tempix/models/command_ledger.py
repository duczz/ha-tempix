"""CommandLedger models — ExpectedHardwareState.

Canonical vocabulary: ``PENDING`` (waiting for HA),
``CONFIRMED`` (acked by HA), ``STALE`` (confirmed but older than
``staleness_timeout``), ``EXPIRED`` (TTL elapsed without confirmation →
resend, explicitly NO re-evaluation).

The dispatcher owns the mutable ledger; the SnapshotBuilder receives an
IMMUTABLE copy (``LedgerView``) for the override detection —
that copy is what these frozen models represent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any


class LedgerEntryState(Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    STALE = "stale"
    EXPIRED = "expired"


def _parse_dt(raw: Any) -> datetime | None:
    """Parse a stored datetime (isoformat str or ``datetime``) to aware-UTC;
    ``None``/garbage → ``None``. Naive values are coerced to UTC so a later
    aware/naive subtraction in :meth:`LedgerEntry.state_at` cannot raise."""
    moment: datetime | None = None
    if isinstance(raw, str):
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            return None
    elif isinstance(raw, datetime):
        moment = raw
    if moment is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


@dataclass(frozen=True)
class LedgerEntry:
    """Last command sent to one device + its confirmation lifecycle."""

    entity_id: str
    hvac_mode: str | None
    target_temperature: float | None
    sent_at: datetime
    # TTL for the confirmation (two-phase commit protection).
    ttl: timedelta
    confirmed_at: datetime | None = None
    # Restore marker: an entry rehydrated from Store after
    # a reboot is forced STALE — its last-sent command is trusted only for
    # value-based echo matching, never re-confirmed and never
    # resent (STALE, unlike EXPIRED, stays out of the reconcile/resend loop).
    stale: bool = False

    def state_at(self, now: datetime, staleness_timeout: timedelta) -> LedgerEntryState:
        """Lifecycle state at ``now`` (pure — clock comes from outside)."""
        if self.stale:
            return LedgerEntryState.STALE
        if self.confirmed_at is None:
            if now - self.sent_at >= self.ttl:
                return LedgerEntryState.EXPIRED
            return LedgerEntryState.PENDING
        if now - self.confirmed_at >= staleness_timeout:
            return LedgerEntryState.STALE
        return LedgerEntryState.CONFIRMED

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the ledger Store."""
        return {
            "entity_id": self.entity_id,
            "hvac_mode": self.hvac_mode,
            "target_temperature": self.target_temperature,
            "sent_at": self.sent_at.isoformat(),
            "ttl_seconds": self.ttl.total_seconds(),
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "stale": self.stale,
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, Any] | None, *, force_stale: bool = False
    ) -> LedgerEntry | None:
        """Hard migration: any missing/broken required field → ``None`` (skip
        this entry, never crash the whole restore). ``force_stale`` overrides
        the stored flag — used by the reboot restore to mark every rehydrated
        entry STALE.
        """
        if not isinstance(data, dict):
            return None

        entity_id = data.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            return None

        sent_at = _parse_dt(data.get("sent_at"))
        if sent_at is None:
            return None  # an entry we can't time is useless for echo matching

        raw_ttl = data.get("ttl_seconds")
        ttl = timedelta(seconds=float(raw_ttl)) if isinstance(raw_ttl, (int, float)) else timedelta(seconds=60)

        hvac_mode = data.get("hvac_mode")
        raw_temp = data.get("target_temperature")
        return cls(
            entity_id=entity_id,
            hvac_mode=str(hvac_mode) if hvac_mode is not None else None,
            target_temperature=(
                float(raw_temp) if isinstance(raw_temp, (int, float)) else None
            ),
            sent_at=sent_at,
            ttl=ttl,
            confirmed_at=_parse_dt(data.get("confirmed_at")),
            stale=True if force_stale else bool(data.get("stale", False)),
        )


@dataclass(frozen=True)
class LedgerView:
    """Immutable per-room ledger copy handed to the SnapshotBuilder."""

    entries: tuple[LedgerEntry, ...] = field(default=())

    def for_entity(self, entity_id: str) -> LedgerEntry | None:
        for entry in self.entries:
            if entry.entity_id == entity_id:
                return entry
        return None
