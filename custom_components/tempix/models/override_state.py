"""ActiveOverride — the persisted temporary manual override.

Designed to be loaded from the HA ``Store``; today the only producer is
``ParallelValidator._mirror_override`` (V1 mirror, see
``ManualOverridePolicy``). V1 lesson (fingerprint bug): the validity
boundary is stored EXPLICITLY as ``expires_at`` (computed from
room-filtered calendar/scheduler transitions at creation time — never
from calendar-entity attributes) instead of being re-derived via
state fingerprints.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from custom_components.tempix.models.system_context import SystemContext


def _parse_dt(raw: Any) -> datetime | None:
    """Parse a stored datetime (isoformat str or ``datetime``) to aware-UTC.

    Tolerates ``None``/garbage → ``None``. A naive value (older schema or a
    manual store edit) is coerced to UTC so a later aware/naive subtraction
    in :meth:`ActiveOverride.is_expired` can never raise ``TypeError`` — the
    same migration contract :class:`SystemContext` promises.
    """
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
class ActiveOverride:
    """Immutable view of the currently active override."""

    override_id: str
    hvac_mode: str | None
    target_temperature: float | None
    created_at: datetime
    # Explicit TTL: the next schedule/calendar transition at creation
    # time. None = no known boundary (the 24h SystemContext net still caps).
    expires_at: datetime | None
    # Frozen creation context; a context change invalidates the override.
    context: SystemContext

    def is_expired(self, now: datetime) -> bool:
        """TTL or hard 24h safety net reached."""
        if self.expires_at is not None and now >= self.expires_at:
            return True
        return self.context.is_expired(now)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the shared domain store."""
        return {
            "override_id": self.override_id,
            "hvac_mode": self.hvac_mode,
            "target_temperature": self.target_temperature,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "context": self.context.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ActiveOverride | None:
        """Hard migration: any missing/broken required field → no override
        (``None``), never a crash. Datetimes are coerced to aware-UTC.

        Returns ``None`` (not a default instance) because an override is
        optional — a corrupted or absent blob means "no active override",
        the safe state.
        """
        if not isinstance(data, dict):
            return None

        override_id = data.get("override_id")
        if not isinstance(override_id, str) or not override_id:
            return None

        created_at = _parse_dt(data.get("created_at"))
        if created_at is None:
            return None  # an override without a trustworthy creation time is untrusted

        hvac_mode = data.get("hvac_mode")
        raw_temp = data.get("target_temperature")
        return cls(
            override_id=override_id,
            hvac_mode=str(hvac_mode) if hvac_mode is not None else None,
            target_temperature=(
                float(raw_temp) if isinstance(raw_temp, (int, float)) else None
            ),
            created_at=created_at,
            expires_at=_parse_dt(data.get("expires_at")),
            context=SystemContext.from_dict(data.get("context")),
        )
