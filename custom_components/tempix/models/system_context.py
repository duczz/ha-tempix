"""SystemContext — frozen context of an active override.

Frozen when an override is created (today: by
``ParallelValidator._mirror_override``); ``ManualOverridePolicy`` compares
it against the live snapshot. If the live context later differs
(away-mode change, new schedule/calendar phase), the override is
invalidated. V1 lesson: no fingerprint heuristics — the validity
boundary (TTL) is stored explicitly; this model only carries the frozen
comparison context.

Persistence (once V2 owns the override): the context is stored via HA
``Store``; ``from_dict`` MUST
tolerate missing/unknown keys with hard defaults so old persisted data
never crashes a future version (migration logic lives here at the model,
nowhere else). A hard 24h ``max_age_hours`` fallback caps any override
whose TTL was lost or corrupted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# Safety net: an override older than this is expired regardless of TTL.
MAX_AGE_HOURS = 24


def _ensure_aware(moment: datetime | None) -> datetime | None:
    """Coerce a datetime to aware-UTC. A naive value (older schema, manual
    store edit, or any writer that used ``datetime.now()``) would otherwise
    raise ``TypeError`` when subtracted from an aware ``now`` in
    :meth:`SystemContext.is_expired` — a crash the module's own migration
    contract promises never to cause."""
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


@dataclass(frozen=True)
class SystemContext:
    """Immutable snapshot of the system state at override creation."""

    # Stable identifier of the active schedule phase (scheduler entity or
    # room-filtered calendar phase) — NOT an entity-attribute fingerprint.
    schedule_id: str | None = None
    away: bool = False
    # Active room modes at creation time (e.g. ("party_mode",)).
    active_modes: tuple[str, ...] = ()
    created_at: datetime | None = None

    def is_expired(self, now: datetime) -> bool:
        """Hard 24h safety net, independent of the explicit override TTL."""
        created_at = _ensure_aware(self.created_at)
        if created_at is None:
            return True  # unknown age = untrusted → expire
        return now - created_at > timedelta(hours=MAX_AGE_HOURS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "away": self.away,
            "active_modes": list(self.active_modes),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SystemContext:
        """Hard migration: defaults for every missing/broken field."""
        if not isinstance(data, dict):
            return cls()

        created_at: datetime | None = None
        raw_created = data.get("created_at")
        if isinstance(raw_created, str):
            try:
                created_at = datetime.fromisoformat(raw_created)
            except ValueError:
                created_at = None
        elif isinstance(raw_created, datetime):
            created_at = raw_created
        created_at = _ensure_aware(created_at)

        raw_modes = data.get("active_modes")
        active_modes = (
            tuple(str(m) for m in raw_modes) if isinstance(raw_modes, (list, tuple)) else ()
        )

        schedule_id = data.get("schedule_id")
        return cls(
            schedule_id=str(schedule_id) if schedule_id is not None else None,
            away=bool(data.get("away", False)),
            active_modes=active_modes,
            created_at=created_at,
        )
