"""TempixSnapshot — the immutable engine input.

Hard architecture rules:
- Only the SnapshotBuilder may read HA state; nothing HA-typed enters
  the snapshot — primitives and frozen models only.
- The evaluation timestamp is frozen in here via the injected
  ``EngineClock``; policies never call ``datetime.now()``.
- Completeness (V1 lesson): every entity the engine needs must be in
  the snapshot. There is NO live fallback — the builder enforces this.

This module defines the base fields. The
domain payload below (schedule state, presence, window, per-sensor health
map, device states, …) is resolved by the SnapshotBuilder, the only
producer of this type.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from custom_components.tempix.models.device_state import DeviceState
from custom_components.tempix.models.health import HealthStatus, SnapshotValidity
from custom_components.tempix.models.override_state import ActiveOverride
from custom_components.tempix.models.system_context import SystemContext
from custom_components.tempix.models.trigger_event import TriggerEvent

# Re-exports: canonical import location for both enums.
__all__ = [
    "HealthStatus",
    "OverrideDetectionResult",
    "SnapshotValidity",
    "TempixSnapshot",
]


@dataclass(frozen=True)
class OverrideDetectionResult:
    """Result of the 5-point override check (built by the
    SnapshotBuilder from TriggerEvent + CommandLedger copy)."""

    matched: bool
    timestamp: float
    calibration_explained: bool
    source: str  # "ui" | "physical"


@dataclass(frozen=True)
class TempixSnapshot:
    """Immutable, complete input for exactly one engine evaluation."""

    snapshot_id: str
    # Frozen evaluation time (UTC) from the injected EngineClock.
    timestamp: datetime
    # What woke the engine ("state_change", "heartbeat", "reload", …).
    trigger_source: str
    validity: SnapshotValidity = SnapshotValidity.VALID
    # Dynamic sensor data read at build time (override check: is the
    # difference explained by a known calibration offset?).
    calibration_offset: float = 0.0
    # Frozen context of the ACTIVE override (persisted) — never rebuilt
    # from live state.
    system_context: SystemContext | None = None
    # Event delta of the trigger; None for heartbeat/timer cycles →
    # override detection does not run.
    trigger_event: TriggerEvent | None = None
    # Result of the override detection — exclusively anchored here.
    detected_override_event: OverrideDetectionResult | None = None

    # ── domain payload (resolved by the SnapshotBuilder) ──────────────────
    # All entity resolution (window quorum, reaction delays, holiday/
    # calendar filtering, guest folding) happens in the builder — policies
    # see only these resolved primitives.
    #
    # ACHTUNG, `None` ist NICHT einheitlich. Hier
    # stand frueher pauschal "`None` = uncertain; der Builder spiegelt das in
    # `validity`". Das gilt nur fuer einen Teil der Felder — und genau diese
    # Pauschale hat einen Fehler ermoeglicht: `schedule_comfort=None` lief mit
    # `validity=VALID` durch, weil alle davon ausgingen, die Spiegelung
    # passiere ohnehin. Welches Feld was meint, steht maschinenlesbar in
    # `UNCERTAIN_WHEN_NONE` / `ABSENT_WHEN_NONE` am Modulende; ein neues
    # Optional-Feld muss dort eingeordnet werden, sonst faellt der Test.

    # Master gate: automation switch AND season mode AND outside-temp gate.
    automation_active: bool = True
    # ONE of the three inputs into
    # `automation_active` above, exposed separately because `_build_reason()`
    # names each of the three master-gate causes individually. Defaults True
    # (V1's own contract: unconfigured or unreadable season-mode entity means
    # "assume active", never lowers `validity`). Read by `binary_sensor.py`
    # and `climate.py`.
    season_mode: bool = True
    # One confirmed-open sensor (after its reaction delay) means open; fewer
    # than half the sensors reachable → None.
    window_open: bool | None = False
    # The builder's own window resolution, not overwritten by the V1 bridge:
    # same rule as ``window_open``, but below the reachability quorum it holds
    # the last valid state instead of returning None. ``window_open`` stays
    # V1-identical on purpose (parity: both engines must see identical inputs
    # for a fair DecisionDiff), and V2's WindowConstraint reads its ``None``
    # as "no inhibit" — dropping exactly the hold this field preserves. Read
    # by WindowConstraint and hardware_dispatcher's window_holds_on; every
    # other consumer (binary_sensor, sensor attributes) keeps using
    # ``window_open``. Always a real bool: on a cold start with no remembered
    # state it falls back to "closed".
    window_open_resolved: bool = False
    # Presence incl. guest mode (guest ⇒ treated as somebody home).
    anybody_home: bool | None = True
    # Display-only — feeds binary_sensor.py, no Policy/
    # DecisionEngine consumer. Resolved natively (resolve_presence_active).
    presence_active: bool | None = None
    # Same story — the raw V1-bridged
    # engine.check_proximity_arrived() value.
    proximity_arrived: bool | None = None
    # Resolved away state (presence-based; feeds SystemContext compare).
    away: bool = False
    vacation_active: bool = False
    vacation_temperature: float | None = None
    party_active: bool = False
    party_temperature: float | None = None
    party_ends_at: datetime | None = None
    force_comfort: bool = False
    force_eco: bool = False
    liming_active: bool = False
    liming_ends_at: datetime | None = None
    # Resolved scheduler/calendar comfort phase (incl. holiday/use_day).
    schedule_comfort: bool | None = None
    # Next phase boundary from ROOM-FILTERED events (never from
    # calendar-entity attributes).
    next_schedule_transition: datetime | None = None
    # Stable id of the current schedule phase (SystemContext compare).
    schedule_phase_id: str | None = None
    # JSON adjustment: "auto" | "comfort" | "eco" | "off" + value overrides.
    adjustment_mode: str = "auto"
    adjustment_comfort: float | None = None
    adjustment_eco: float | None = None
    # Resolved base temperatures (static config, number entity or
    # calendar override — dynamic, therefore snapshot data).
    comfort_temperature: float | None = None
    eco_temperature: float | None = None
    sunshine_active: bool = False
    # The actual offset value
    # (`sunshine_active` above is only the bool). Already 0.0 when inactive
    # (V1's own `get_sunshine_offset()` contract) — never uncertain, no
    # UNCERTAIN_WHEN_NONE/ABSENT_WHEN_NONE entry needed.
    sunshine_offset: float = 0.0
    # Reine Anzeige-Felder fuer
    # `_build_reason()`s Kalender-/Feiertags-Zweige (AWAY/COMFORT/ECO) — beide
    # bereits nativ in `domain_resolvers.py` (`resolve_calendar_tags`,
    # `resolve_holiday_today`). Nie unsicher (leeres Dict/`False` = "kein Tag
    # gefunden"/"kein Feiertag"), deshalb kein Eintrag in UNCERTAIN_WHEN_NONE
    # /ABSENT_WHEN_NONE noetig — keines der beiden ist ein Optional-Feld.
    calendar_tags: dict = field(default_factory=dict)
    # Der Kalender-`hvac:`-Tag als ENTSCHEIDUNGSwert (nicht
    # Anzeige) — bereits gegen die bekannten Modi geprueft (Rohwerte ausserhalb
    # der Menge kommen hier als None an, ParallelValidator warnt einmal pro
    # Wert). `None` =
    # kein (gueltiger) Tag im aktiven Termin, also ein legitimer Zustand.
    calendar_hvac_mode: str | None = None
    holiday_today: bool = False
    # Smart preconditioning: builder computes the lookahead (needs
    # learned rate + next comfort transition); the policy consumes the flag.
    preconditioning_active: bool = False
    room_temperature: float | None = None
    # The BUILDER's OWN room-temperature
    # resolution (external sensor, falling back to the device's own
    # current_temperature) — NOT overwritten by the V1 bridge above.
    # ``room_temperature`` stays V1-identical on purpose (parity; a
    # test measures that removing that override would open a wide, everyday V1/V2 diff, not just a Frost gap).
    # This field exists so FrostConstraint's OFF-facet can see the room is
    # actually cold even when no external sensor is configured — the common
    # case where ``room_temperature`` is always None (V1 has no device
    # fallback at all) and Frost's setpoint-injection could never fire.
    # Read by FrostConstraint, FrostGoal and the dispatcher's frost-floor
    # exception; every other consumer (valve
    # positioning, the DecisionDiff comparison) intentionally keeps using
    # ``room_temperature`` above.
    room_temperature_resolved: float | None = None
    # A configured EXTERNAL room sensor is unavailable this cycle. V1
    # (engine_calibration.calculate_changes) hard-aborts device planning in
    # this case (freezes all valves); the dispatcher mirrors that. Stays
    # False when no external sensor is configured (device sensor is truth).
    room_sensor_failed: bool = False
    # Persisted temporary manual override, loaded by the builder.
    active_override: ActiveOverride | None = None
    # Configured entity references
    # that no longer exist (deleted persons/calendars/sensors/…) — a
    # CONFIGURATION fault, not a runtime one, so it never lowers `validity`
    # (same distinction as `engine_base.get_missing_entity_references`'s
    # docstring). Never None (empty tuple = nothing missing), so no
    # UNCERTAIN_WHEN_NONE/ABSENT_WHEN_NONE entry needed.
    # NOT YET READ by any coordinator.py call site (the obvious one,
    # `_track_dead_references()`, is unsafe to migrate — see
    # `domain_resolvers.resolve_missing_entity_references`'s docstring).
    # Verified-correct groundwork (real parity test against the V1 mixin),
    # kept for whenever a per-cycle-fresh, `_should_run_v2`-independent
    # consumer exists.
    missing_entity_references: tuple[str, ...] = field(default=())

    # ── raw layer (completeness contract) ─────────────────────────────────
    # Snapshotted state of every managed climate device (dry-normalized).
    device_states: tuple[DeviceState, ...] = field(default=())
    # Health of EVERY declared entity (incl. holiday calendar and
    # adjustment-referenced entities). There is no live fallback: an
    # entity missing here is UNKNOWN — by definition, not by accident.
    sensor_health: tuple[tuple[str, HealthStatus], ...] = field(default=())
    # Raw state strings of the declared entities (diagnostics/resolvers).
    entity_values: tuple[tuple[str, str | None], ...] = field(default=())
    # WARUM die Validity nicht VALID ist — im Klartext, in derselben
    # Reihenfolge, in der der Builder sie sammelt. Frueher landeten
    # diese Gruende nur in einer DEBUG-Logzeile: der Diagnose-Export zeigte
    # ein nacktes "degraded" ohne Erklaerung, und der Grund liess sich nur
    # ueber die Sensorzustaende rueckwaerts erschliessen. Post-Flip ist
    # `validity` das zentrale Feld fuer "warum tut mein Raum nichts" — ohne
    # Begruendung ist es eine Sackgasse.
    validity_reasons: tuple[str, ...] = field(default=())

    def health_for(self, entity_id: str) -> HealthStatus:
        """Completeness rule: never-snapshotted entities surface as UNKNOWN."""
        for eid, status in self.sensor_health:
            if eid == entity_id:
                return status
        return HealthStatus.UNKNOWN

    def device_state_for(self, entity_id: str) -> DeviceState | None:
        for device in self.device_states:
            if device.entity_id == entity_id:
                return device
        return None


# ── Was `None` bei welchem Feld bedeutet ─────────────────────────────────────
#
# Der rote Faden hinter mehreren Fehlern (`schedule_comfort`, `away`, Fenster,
# Proximity) war immer derselbe: Unsicherheit wurde als Wert
# gerendert. Die Gegenmassnahme beginnt damit, ueberhaupt festzuhalten, WO
# `None` "ich weiss es nicht" heisst und wo es "gibt es nicht" heisst — im
# Modell sehen beide identisch aus.
#
# Beispiel fuer die Verwechslungsgefahr, die diese Liste aufloest:
#   room_temperature = None  →  KEINE QUELLE, der Raum ist ungeregelt (UNSAFE)
#   party_temperature = None →  keine Party, voellig belanglos
#
# Die Defaults sind zusaetzlich uneinheitlich und lassen sich nicht mehr
# aendern, ohne Verhalten zu drehen: `window_open=False`, `anybody_home=True`,
# `schedule_comfort=None`. Wer ein Feld liest, muss deshalb die Bedeutung
# kennen — raten reicht nicht.

#: `None` heisst UNBEKANNT. Der Builder MUSS das in die Validity spiegeln
#: (DEGRADED oder UNSAFE) und einen Grund nennen, sonst entscheidet die
#: Pipeline auf einer erfundenen Aussage.
UNCERTAIN_WHEN_NONE: frozenset[str] = frozenset({
    "window_open",          # weniger als die Haelfte erreichbar (Quorum)
    "anybody_home",         # Person-Entities unsicher
    # "nicht pruefbar" wie anybody_home — reine Anzeige-Werte, noch ohne
    # Policy-Konsument (im Test als NOT_YET_MIRRORED gefuehrt).
    "presence_active",
    "proximity_arrived",
    "schedule_comfort",     # Zeitplan-/Kalenderphase nicht bestimmbar
    "room_temperature",     # keine Temperaturquelle → UNSAFE
    # Identische Bedeutung wie room_temperature (beide None genau dann, wenn
    # der Builder ueberhaupt keine Quelle findet — auch nicht das Geraet) —
    # separat gelistet, weil es ein eigenes Optional-Feld ist.
    "room_temperature_resolved",
    "comfort_temperature",  # Sollwert nicht aufloesbar (z.B. Number-Entity weg)
    "eco_temperature",
})

#: `None` heisst ABWESEND — ein legitimer Zustand, keine Unsicherheit. Diese
#: Felder duerfen die Validity NICHT senken; taeten sie es, waere jeder Raum
#: ohne Party dauerhaft "degraded".
ABSENT_WHEN_NONE: frozenset[str] = frozenset({
    "vacation_temperature",     # kein Urlaubs-Sollwert konfiguriert
    "party_temperature",        # keine Party
    "party_ends_at",
    "liming_ends_at",
    "next_schedule_transition", # kein weiterer Phasenwechsel bekannt
    "schedule_phase_id",        # beim Boot noch unbekannt; Vergleiche gaten
                                # explizit auf "beide Seiten kennen ihre Phase"
    "adjustment_comfort",       # kein Adjustment-Override
    "adjustment_eco",
    "active_override",          # keine temporaere Uebersteuerung
    "calendar_hvac_mode",       # kein (gueltiger) `hvac:`-Tag im aktiven Termin
    # Meta-/Rohschicht: nie Domaenen-Aussagen
    "system_context",
    "trigger_event",
    "detected_override_event",
    "entity_values",
})
