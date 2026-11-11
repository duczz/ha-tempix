"""SnapshotBuilder — the anti-corruption layer.

Turns live HA state into a fully primitive ``TempixSnapshot``; no
``State``/``Entity`` reference survives the build. The policies and the
DecisionEngine read only the snapshot. The evaluation timestamp comes from the injected
``EngineClock``.

Responsibilities:
- safe parsing of unavailable/unknown states (no ``ValueError``),
  per-entity ``HealthStatus`` classification;
- Policy Resolver: window fallback (hold the last valid sensor
  state, cold start → closed), room-temp fallback onto the device's
  own sensor (→ DEGRADED), aggregated ``SnapshotValidity`` up to UNSAFE;
- hard dry-mode normalization (non-temperature mode ⇒ target ``None``);
- override detection: the **5-point check** against an immutable
  ``LedgerView``, result anchored exclusively on the snapshot;
- completeness contract: every declared entity is snapshotted with
  health + raw value; reading an undeclared entity raises — there is NO
  live fallback.

High-level domain resolution (schedule phase, party timers, calendar
transitions, …) arrives as pre-resolved keyword fields on
:meth:`build` from ``ParallelValidator._build_snapshot`` (mostly native
``domain_resolvers``, the rest bridged from V1 via ``V2Bridge``).
"""
from __future__ import annotations

import dataclasses
import logging
import math
import uuid
from datetime import datetime, timedelta
from typing import Any

from custom_components.tempix.clock import EngineClock
from custom_components.tempix.models.command_ledger import (
    LedgerEntry,
    LedgerEntryState,
    LedgerView,
)
from custom_components.tempix.models.device_state import DeviceState
from custom_components.tempix.models.health import HealthStatus, SnapshotValidity
from custom_components.tempix.models.snapshot import (
    OverrideDetectionResult,
    TempixSnapshot,
)
from custom_components.tempix.models.target_state import TEMPERATURE_DRIVEN_HVAC_MODES
from custom_components.tempix.models.trigger_event import TriggerEvent

_LOGGER = logging.getLogger(__name__)

# States that carry no usable value (safe-parsing contract).
INVALID_STATES = frozenset({"unavailable", "unknown", "none", ""})

# Override-Erkennung, Kriterium 1: TRV-Rundungstoleranz.
#
# BEWUSSTE DESIGN-GRENZE: 0.5 °C ist bei den
# meisten TRVs EXAKT ein Schritt. Eine echte Einzelschritt-Drehung des
# Nutzers kurz nach einem eigenen Kommando wird deshalb als Echo gewertet —
# der Eingriff geht verloren. Das ist der Preis dafuer, dass nicht jedes
# Geraete-Echo als manueller Eingriff zaehlt. Das Gegenstueck in V1,
# `TempixCoordinator._temps_match`, importiert diese Konstante — ein Wert
# fuer beide Engines.
TEMP_TOLERANCE = 0.5

_WINDOW_OPEN_STATES = frozenset({"on", "open", "tilted"})
_WINDOW_CLOSED_STATES = frozenset({"off", "closed"})

# Valid field names for the domain-override typo guard (see build()).
_SNAPSHOT_FIELD_NAMES = frozenset(f.name for f in dataclasses.fields(TempixSnapshot))


def update_window_memory(
    memory: dict[str, str], snapshot: TempixSnapshot, window_sensors: tuple[str, ...],
) -> None:
    """Record each window sensor's current VALID state for the next cycle's
    reboot-safe close-delay. An ``unavailable``/``unknown`` reading is
    skipped — it must NOT overwrite the last known valid state, otherwise
    the flap would erase the very memory the reboot guard relies on."""
    values = dict(snapshot.entity_values)
    for sid in window_sensors:
        raw = values.get(sid)
        if raw is None:
            continue
        low = raw.lower()
        if low in INVALID_STATES:
            continue
        memory[sid] = low


def _as_temp_sensor_tuple(value: Any) -> tuple[str, ...]:
    """Every sensor `temp_sensor` (``str | list | None``) may name — mirrors
    ``engine_base._resolve_room_temp``'s own normalization exactly (nested
    list flatten, comma-split string, single fallback)."""
    if not value:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(
            str(s).strip()
            for item in value
            for s in ([item] if not isinstance(item, (list, tuple)) else item)
            if s
        )
    return tuple(s.strip() for s in str(value).split(",") if s.strip())


def _as_entity_tuple(values: Any) -> tuple[str, ...]:
    """Flatten a (possibly nested / mixed) entity-ref collection into a flat
    tuple of non-empty entity-id strings — so ``_declared`` never contains
    an unhashable list (real crash: a single-element list config)."""
    out: list[str] = []
    for v in values or ():
        if isinstance(v, (list, tuple)):
            out.extend(str(x) for x in v if x)
        elif v:
            out.append(str(v))
    return tuple(out)


def parse_float(value: Any) -> float | None:
    """Parse a state/attribute into float — never raises.

    ``float()`` happily parses "nan"/"inf" — a NaN then survives
    every downstream comparison (always False) and an Infinity satisfies
    every threshold, either silently or via ``int(nan + 0.5)`` crashing the
    whole ``_do_update`` cycle. Neither is a real sensor reading.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = float(value)
        return value if math.isfinite(value) else None
    try:
        text = str(value).strip()
        if text.lower() in INVALID_STATES:
            return None
        parsed = float(text)
        return parsed if math.isfinite(parsed) else None
    except (ValueError, TypeError):
        return None


def _temps_match(a: float | None, b: float | None, tolerance: float = TEMP_TOLERANCE) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tolerance


def _modes_match(a: str | None, b: str | None) -> bool:
    return a == b


def _fuse_room_temps(vals: list[float]) -> float:
    """Median with an outlier filter — mirrors engine_base._resolve_room_temp
    exactly, so this builder's own shadow resolve agrees with V1's real fusion whenever all
    the same sensors are healthy. One shared algorithm, two independent
    per-module implementations (different raw-state access patterns), kept
    in sync deliberately rather than cross-importing between engine_base and
    snapshot_builder."""
    if len(vals) == 1:
        return vals[0]
    sorted_vals = sorted(vals)
    median = sorted_vals[len(sorted_vals) // 2]
    max_dev = 3.0
    filtered = [v for v in vals if abs(v - median) <= max_dev]
    return sum(filtered) / len(filtered) if filtered else median


class SnapshotBuilder:
    """Builds immutable snapshots from live HA state (one per room)."""

    def __init__(
        self,
        hass: Any,
        clock: EngineClock,
        *,
        device_entities: Any = (),
        temp_sensor: str | list | None = None,
        window_sensors: Any = (),
        extra_entities: Any = (),
        window_reaction_open: timedelta = timedelta(0),
        window_reaction_close: timedelta = timedelta(0),
        staleness_timeout: timedelta = timedelta(seconds=300),
        # A room sensor whose VALUE hasn't
        # moved in this long is STALE even while HA still reports it
        # `available` — a stuck sensor (dead battery, wedged sensor)
        # rarely flips that flag. Deliberately long: a
        # thermally stable room can legitimately hold one reading for
        # hours, this is only meant to catch a sensor stuck for the long
        # haul without false-positiving on a quiet night.
        room_sensor_stale_after: timedelta = timedelta(hours=24),
    ) -> None:
        self._hass = hass
        self._clock = clock
        # Normalize all entity refs: HA config fields are typed
        # str | list | None (a single selector can return ["id"]), so
        # coerce to flat str tuples / a single str before they ever reach
        # the frozenset below (unhashable-list crash).
        self._device_entities = _as_entity_tuple(device_entities)
        # The full fusion set this room's
        # temp_sensor config names, not just its first entry.
        self._temp_sensors = _as_temp_sensor_tuple(temp_sensor)
        self._window_sensors = _as_entity_tuple(window_sensors)
        self._extra_entities = _as_entity_tuple(extra_entities)
        self._window_reaction_open = window_reaction_open
        self._window_reaction_close = window_reaction_close
        self._staleness_timeout = staleness_timeout
        self._room_sensor_stale_after = room_sensor_stale_after
        # The complete, closed set of entities this room may read.
        self._declared: frozenset[str] = frozenset(
            (
                *self._device_entities,
                *self._temp_sensors,
                *self._window_sensors,
                *self._extra_entities,
            )
        )

    @property
    def declared_entities(self) -> frozenset[str]:
        return self._declared

    # ── raw reads (completeness contract) ─────────────────────────────────

    def _get_state(self, entity_id: str) -> Any:
        """Read a DECLARED entity — undeclared access is a bug."""
        if entity_id not in self._declared:
            raise LookupError(
                f"entity '{entity_id}' is not declared for this room — "
                "there is no live fallback; declare it at builder setup"
            )
        return self._hass.states.get(entity_id)

    def _classify(self, state: Any) -> HealthStatus:
        if state is None or str(state.state).lower() in INVALID_STATES:
            return HealthStatus.UNAVAILABLE
        return HealthStatus.AVAILABLE

    def _room_sensor_health(self, state: Any, now: datetime) -> HealthStatus:
        """Room-sensor-only refinement of `_classify`: `HealthStatus.STALE`
        existed in the enum but was never assigned anywhere. Scoped to the room sensor rather than folded into
        `_classify` itself — window/device classification has no
        stuck-but-available failure mode that depends on it, and giving every declared entity a
        staleness opinion would be a much bigger, riskier change. Uses `last_changed` (last time the STATE VALUE
        moved), not `last_updated` (moves on every poll) — a live sensor
        re-publishing the SAME reading must not trip this."""
        base = self._classify(state)
        if base is not HealthStatus.AVAILABLE:
            return base
        last_changed = getattr(state, "last_changed", None)
        if last_changed is not None and now - last_changed >= self._room_sensor_stale_after:
            return HealthStatus.STALE
        return HealthStatus.AVAILABLE

    # ── build ─────────────────────────────────────────────────────────────

    def build(
        self,
        trigger_source: str,
        *,
        trigger_event: TriggerEvent | None = None,
        ledger: LedgerView | None = None,
        pause_active: bool = False,
        calibration_offset: float = 0.0,
        # Last VALID window-sensor states from the previous cycle (sid →
        # state string). Feeds the reboot-safe close-delay; the caller owns
        # this dict across cycles and updates it via update_window_memory().
        previous_window_states: dict[str, str] | None = None,
        # Zeitpunkt (`last_changed`) des letzten ECHTEN offen→zu je Sensor.
        # Der Aufrufer besitzt das Dict ueber Zyklen; `_resolve_window`
        # schreibt es fort. Ohne es hielt der Close-Delay nur bis zum
        # naechsten Zyklus (Live-Test 2026-11-04, F5).
        window_close_marks: dict[str, Any] | None = None,
        # Sensoren, deren aktuelle Oeffnungsphase die Oeffnungs-Reaktionszeit
        # ueberlebt hat (LT-F12-Rest d). Nur dann setzt ein Schliessen eine
        # Marke. Der Aufrufer besitzt das Set; None = keine Pruefung.
        window_open_phase_confirmed: set[str] | None = None,
        # Warum `schedule_comfort` unbekannt ist — falls der Aufrufer es weiss.
        # Der Builder kann das nicht selbst herausfinden: er sieht nur den
        # Wert `None`, nicht die Kette dahinter. Ohne diesen Weg meldet das
        # `room_degraded`-Issue jede Praesenz-Unsicherheit als
        # "schedule state unknown" und schickt den Nutzer zum falschen Sensor.
        comfort_unknown_reason: str | None = None,
        **domain: Any,
    ) -> TempixSnapshot:
        """Assemble one immutable snapshot for one evaluation."""
        now = self._clock.now()
        ledger = ledger if ledger is not None else LedgerView()
        degraded_reasons: list[str] = []
        unsafe_reasons: list[str] = []

        # 1. raw layer: health + raw values of ALL declared entities
        raw: dict[str, Any] = {eid: self._get_state(eid) for eid in self._declared}
        sensor_health = tuple(
            (eid, self._classify(state)) for eid, state in sorted(raw.items())
        )
        entity_values = tuple(
            (eid, None if state is None else str(state.state))
            for eid, state in sorted(raw.items())
        )

        # 2. device states incl. hard dry normalization
        device_states = tuple(
            self._read_device(raw[eid], eid) for eid in self._device_entities
        )

        # 3. window resolution (fallback + reboot-safe close-delay)
        window_open, window_unresolved = self._resolve_window(
            raw, now, previous_window_states or {},
            window_close_marks if window_close_marks is not None else {},
            window_open_phase_confirmed)
        if window_unresolved:
            # Sensoren benennen (gleiche Schreibweise wie der Praesenz-Grund in
            # parallel_validation): das Repair sagt "Pruefe den betroffenen
            # Sensor" — ohne Entity-ID war das nicht befolgbar.
            degraded_reasons.append(
                f"window sensors unresolvable "
                f"({', '.join(window_unresolved[:3])}) → last known state held")

        # 4. room temperature resolution (device-sensor fallback)
        room_temp, temp_status = self._resolve_room_temp(raw, device_states, now)

        # 4a. external room sensor availability (V1 planning hard-abort).
        # Must reflect the FUSION, not just the first sensor — a room
        # with one healthy sensor out of several still has a usable room
        # temperature (median fusion, engine_base._resolve_room_temp) and
        # must not freeze every command just because sensor [0] is down.
        # Computed BEFORE the DEGRADED message below, because that message
        # must name this consequence instead of a fallback that never
        # actually reaches the room (see the note there).
        room_sensor_failed = bool(self._temp_sensors) and all(
            self._classify(raw[sid]) is HealthStatus.UNAVAILABLE
            for sid in self._temp_sensors
        )

        if temp_status is SnapshotValidity.DEGRADED:
            # Distinguish "sensor offline" from "sensor stuck but still
            # reporting" — same DEGRADED outcome, different repair for the user.
            stale = any(
                self._room_sensor_health(raw[sid], now) is HealthStatus.STALE
                for sid in self._temp_sensors
            )
            # "fell back to device sensor" is this BUILDER's own shadow
            # resolution (overridden for the real decision by V1's fused
            # value, parallel_validation.py). When every
            # configured sensor is down, that fallback is fiction — plan()
            # freezes every setpoint (except the frost floor); only valve
            # positions still go out (plan_valves). Name that instead.
            if room_sensor_failed:
                degraded_reasons.append(
                    "room temp sensor unavailable → setpoints frozen for this room"
                )
            else:
                # Stale-Text: nennt den Befund, nicht den Fallback.
                # Raumsensor und Geraete-Fuehler koennen dieselbe Entity sein
                # (Comet DECT) — "fell back to device sensor" behauptete dann
                # einen Ausweichwert, der keiner war. Das Repair zeigt diesen
                # Text dem Nutzer (`reason_text`), siehe Notiz oben.
                degraded_reasons.append(
                    "room temp sensor unchanged for over "
                    f"{int(self._room_sensor_stale_after.total_seconds() // 3600)}h "
                    "(possibly frozen)"
                    if stale else "room temp fell back to device sensor"
                )
        elif temp_status is SnapshotValidity.UNSAFE:
            unsafe_reasons.append("no temperature source available")

        # 4c. schedule uncertainty. An
        # unresolved scheduler/calendar phase feeds SchedulePolicy's
        # eco-safe branch (``schedule_comfort is True`` — see goals.py),
        # but that docstring's claim that the builder mirrors the
        # uncertainty into SnapshotValidity was false until now: a
        # scheduler helper stuck on unavailable/unknown (HA restart,
        # helper reload, broken integration) produced VALID with zero
        # signal. Only fires when the caller explicitly passed None —
        # callers that never touch schedule_comfort (most window/override
        # unit tests) are unaffected.
        if "schedule_comfort" in domain and domain["schedule_comfort"] is None:
            degraded_reasons.append(
                comfort_unknown_reason
                or "schedule state unknown → eco-safe fallback applied"
            )

        # 5. override detection (5-point check)
        detected = self._detect_override(
            trigger_event, ledger, now,
            pause_active=pause_active,
            calibration_offset=calibration_offset,
        )

        # 6. validity aggregation
        if unsafe_reasons:
            validity = SnapshotValidity.UNSAFE
        elif degraded_reasons:
            validity = SnapshotValidity.DEGRADED
        else:
            validity = SnapshotValidity.VALID
        if degraded_reasons or unsafe_reasons:
            _LOGGER.debug(
                "snapshot validity %s: %s", validity.value,
                "; ".join((*degraded_reasons, *unsafe_reasons)),
            )

        fields: dict[str, Any] = dict(
            snapshot_id=f"snap-{uuid.uuid4().hex[:10]}",
            timestamp=now,
            trigger_source=trigger_source,
            validity=validity,
            validity_reasons=(*degraded_reasons, *unsafe_reasons),
            calibration_offset=calibration_offset,
            trigger_event=trigger_event,
            detected_override_event=detected,
            window_open=window_open,
            # The builder's OWN value (open wins +
            # last-known-state fallback), captured here so it survives the V1
            # override below (parallel_validation.py passes window_open=,
            # never window_open_resolved=).
            window_open_resolved=window_open,
            room_temperature=room_temp,
            # The builder's OWN value, captured here so it
            # survives the V1 override below (parallel_validation.py passes
            # room_temperature=, never room_temperature_resolved=).
            room_temperature_resolved=room_temp,
            room_sensor_failed=room_sensor_failed,
            device_states=device_states,
            sensor_health=sensor_health,
            entity_values=entity_values,
        )
        # Domain kwargs may OVERRIDE computed fields (the V1 bridge feeds
        # V1-resolved window/room values so both engines see identical
        # inputs — no timing artifacts). A typo'd
        # key would otherwise become a bare TypeError at construction and
        # (in the coordinator) get swallowed to a log line — validate the
        # keys here so the failure names the offending field.
        unknown = set(domain) - _SNAPSHOT_FIELD_NAMES
        if unknown:
            raise ValueError(
                f"unknown domain kwargs for TempixSnapshot: {sorted(unknown)}"
            )
        fields.update(domain)
        return TempixSnapshot(**fields)

    # ── device states ─────────────────────────────────────────────────────

    def _read_device(self, state: Any, entity_id: str) -> DeviceState:
        if self._classify(state) is HealthStatus.UNAVAILABLE:
            return DeviceState(
                entity_id=entity_id, hvac_mode=None, target_temperature=None,
                current_temperature=None, health=HealthStatus.UNAVAILABLE,
            )
        hvac_mode = str(state.state)
        target = parse_float(state.attributes.get("temperature"))
        current = parse_float(state.attributes.get("current_temperature"))
        # Hard normalization: non-temperature modes NEVER carry
        # a target — stale attribute values must not poison the snapshot.
        if hvac_mode not in TEMPERATURE_DRIVEN_HVAC_MODES:
            target = None
        return DeviceState(
            entity_id=entity_id, hvac_mode=hvac_mode, target_temperature=target,
            current_temperature=current, health=HealthStatus.AVAILABLE,
        )

    # ── resolvers ─────────────────────────────────────────────────────────

    def _resolve_window(
        self,
        raw: dict[str, Any],
        now: datetime,
        prev_valid_states: dict[str, str],
        close_marks: dict[str, Any] | None = None,
        open_phase_confirmed: set[str] | None = None,
    ) -> tuple[bool | None, tuple[str, ...]]:
        """(window_open, unresolved_sensors). V1 vote semantics + Fallback.

        Das zweite Element nennt die Sensoren, die den DEGRADED-Fall ausgelöst
        haben; leer heißt "nicht degraded" (bleibt damit als Wahrheitswert
        benutzbar). Vorher war es ein blankes Bool — das `room_degraded`-Repair
        forderte "Prüfe den betroffenen Sensor", konnte ihn aber nicht
        benennen, während `room_trv_unavailable` seine Entity nennt.

        Bei ungültigem Sensor wird der letzte gültige Zustand gehalten
        (Begründung unten).

        Reboot-safe (fixes V1's sensor-availability-flap edge, real
        case): the close-delay ("a just-closed window still counts as
        open for a while") fires ONLY after a genuine open→closed
        transition. A sensor that flapped ``off → unavailable → off``
        (router/sensor reboot) gets a fresh ``last_changed`` but was never
        actually open, so ``prev_valid_states`` (last VALID state before any
        unavailable gap) gates it — the window was closed the whole time and
        must NOT be treated as open.

        ``close_marks`` (sid → ``last_changed`` des echten offen→zu) haelt den
        Delay ueber den ersten Zyklus hinaus: ``prev_valid_states`` steht ab
        dem zweiten Zyklus nach dem Schliessen schon auf ``closed``. Die Marke
        gilt nur, solange der Sensor noch auf GENAU diesem ``last_changed``
        steht — ein spaeterer Flap bekommt einen frischen Zeitstempel und
        verlaengert nichts (Live-Test 2026-11-04, F5; gotchas #72).

        ``open_phase_confirmed`` (LT-F12-Rest d; gotchas #75): Sensoren, deren
        Oeffnungsphase ``window_reaction_open`` ueberlebt hat (oder per
        Wiederoeffnen-Klausel als offen galt). Nur deren Schliessen setzt eine
        Marke; Prellen setzt keine. ``None`` = keine Pruefung. Zusaetzlich (b):
        ein junges Oeffnen kurz nach der Marke eines ANDEREN Sensors des Raums
        gilt als offen. Marken bestaetigter Sensoren werden erst nach der
        Schleife geloescht (sonst haengt (b) an der Sensorreihenfolge).
        """
        if close_marks is None:
            close_marks = {}
        sensors = self._window_sensors
        if not sensors:
            return False, ()

        open_confirmed = False
        closed_recent = False
        unresolved: list[str] = []
        open_deadline = now - self._window_reaction_open
        close_deadline = now - self._window_reaction_close
        close_delta = self._window_reaction_close
        # LT-F12-Rest (d): ohne Phasen-Set (None) keine Pruefung = altes Verhalten.
        phases = open_phase_confirmed
        confirmed_now: list[str] = []
        pending_reopen: list[tuple[str, Any]] = []

        for sid in sensors:
            state = raw[sid]
            if self._classify(state) is HealthStatus.UNAVAILABLE:
                unresolved.append(sid)
                continue
            value = str(state.state).lower()
            last_changed = getattr(state, "last_changed", None)
            prev_open = prev_valid_states.get(sid) in _WINDOW_OPEN_STATES
            if value in _WINDOW_OPEN_STATES:
                if phases is not None and not prev_open:
                    # neue Oeffnungsphase: noch unbestaetigt
                    phases.discard(sid)
                # reaction delay: open only counts once it lasted long enough
                if last_changed is None or last_changed <= open_deadline:
                    open_confirmed = True
                    confirmed_now.append(sid)
                    if phases is not None:
                        phases.add(sid)
                else:
                    # LT-F12: Wiederoeffnen waehrend der laufenden
                    # Schliessverzoegerung gilt lueckenlos als offen (die
                    # Oeffnungs-Reaktionszeit ist hier kein Praellschutz,
                    # das Fenster war ja gerade noch "offen"). Die Marke
                    # bleibt bis zum bestaetigten Oeffnen, sonst waere das
                    # Fenster ab dem zweiten Zyklus wieder "zu".
                    mark = close_marks.get(sid)
                    if (
                        close_delta > timedelta(0)
                        and mark is not None
                        and timedelta(0) <= last_changed - mark <= close_delta
                    ):
                        open_confirmed = True
                        if phases is not None:
                            phases.add(sid)  # Klausel-offen gilt als bestaetigt
                    else:
                        pending_reopen.append((sid, last_changed))
            elif value in _WINDOW_CLOSED_STATES:
                # close delay applies only to a REAL open→closed transition:
                # the previous valid state must have been open. Otherwise a
                # closed sensor that merely flapped through 'unavailable' would
                # falsely count as open (reboot edge).
                was_open = prev_open
                # LT-F12-Rest (d): nur eine bestaetigte Oeffnungsphase setzt
                # eine Marke (kurzes Oeffnen+Schliessen = Prellen, kein Delay).
                if (
                    was_open
                    and last_changed is not None
                    and (phases is None or sid in phases)
                ):
                    close_marks[sid] = last_changed
                genuine_close = (
                    last_changed is not None
                    and close_marks.get(sid) == last_changed
                )
                if (
                    genuine_close
                    and last_changed >= close_deadline
                ):
                    closed_recent = True
                if phases is not None and not was_open:
                    phases.discard(sid)

        # LT-F12-Rest (b): Mehrsensor-Raum — ein Sensor, der kurz nach dem
        # Schliessen eines ANDEREN Sensors des Raums oeffnet, gilt als offen.
        if pending_reopen and close_delta > timedelta(0):
            room_marks = [close_marks[s] for s in sensors if s in close_marks]
            for sid, lc in pending_reopen:
                if any(timedelta(0) <= lc - m <= close_delta for m in room_marks):
                    open_confirmed = True
                    if phases is not None:
                        phases.add(sid)

        for sid in confirmed_now:
            close_marks.pop(sid, None)

        if open_confirmed:
            return True, ()

        valid = len(sensors) - len(unresolved)
        if unresolved and valid * 2 < len(sensors):
            # V1s None-Unsicherheit wurde ursprünglich durch eine
            # KONTEXT-Annahme ersetzt (Komfort-Phase + anwesend → OFFEN, sonst
            # ZU). Heute gilt stattdessen "letzten gültigen Zustand halten".
            # Zwei Gründe:
            #
            # 1. Die Schwelle ist schärfer, als sie klingt: bei EINEM Sensor im
            #    Raum ist `valid * 2 < 1` schon bei einem einzigen Ausfall
            #    erfüllt. Für Ein-Sensor-Räume — der Normalfall — griff die
            #    Annahme also bei jeder leeren Batterie, nicht nur bei einem
            #    Massenausfall.
            # 2. Sie riet aus `schedule_comfort`/`away`, also aus Signalen, die
            #    über den Fensterzustand nichts aussagen — und verwarf dabei die
            #    letzte echte Messung, die hier bereits vorliegt. Der Schaden
            #    skaliert zudem mit der Raumzahl: stirbt der ZigBee-Koordinator,
            #    fielen ALLE Räume der Komfort-Phase auf `window_open_temp`.
            #
            # `prev_valid_states` ist die letzte NICHT-`unavailable` Messung je
            # Sensor (`update_window_memory`), überlebt also genau den Flap, um
            # den es hier geht. Ohne Erinnerung (Kaltstart) bleibt es bei "zu" —
            # das entspricht dem Verhalten, das der Produktivpfad mit V1-Brücke
            # ohnehin fährt (V1 liefert None, und None hemmt in V2 nicht).
            held_open = any(
                prev_valid_states.get(sid) in _WINDOW_OPEN_STATES
                for sid in sensors
            )
            return held_open, tuple(unresolved)

        return closed_recent, ()

    def _resolve_room_temp(
        self, raw: dict[str, Any], device_states: tuple[DeviceState, ...], now: datetime,
    ) -> tuple[float | None, SnapshotValidity]:
        """(room_temperature, contribution to validity).

        Fuses ALL configured sensors (mirrors engine_base._resolve_room_temp's
        median + outlier filter exactly) — not just the first one.
        """
        external_configured = bool(self._temp_sensors)
        if external_configured:
            vals: list[float] = []
            for sid in self._temp_sensors:
                state = raw[sid]
                # STALE falls through, same as UNAVAILABLE — a sensor
                # stuck on one value is no more trustworthy than an offline
                # one.
                if self._room_sensor_health(state, now) is not HealthStatus.AVAILABLE:
                    continue
                value = parse_float(state.state)
                if value is not None:
                    vals.append(value)
            if vals:
                return _fuse_room_temps(vals), SnapshotValidity.VALID

        for device in device_states:
            if device.current_temperature is not None:
                # Fallback on the device's own sensor: DEGRADED when an
                # external sensor was configured but failed (systematic
                # misheating must stay visible), VALID when the device
                # sensor IS the configured truth.
                return (
                    device.current_temperature,
                    SnapshotValidity.DEGRADED if external_configured
                    else SnapshotValidity.VALID,
                )
        return None, SnapshotValidity.UNSAFE

    # ── 5-point override check ───────────────────────────────────────────

    def _detect_override(
        self,
        event: TriggerEvent | None,
        ledger: LedgerView,
        now: datetime,
        *,
        pause_active: bool,
        calibration_offset: float,
    ) -> OverrideDetectionResult | None:
        # Heartbeat/timer cycles carry no event → detection does not run.
        if event is None:
            return None
        # Criterion 4: pause mode never produces overrides (shadow-state
        # prevention; the UI passthrough writes no ledger entries either).
        if pause_active:
            return None
        entry = ledger.for_entity(event.entity_id)
        if entry is None:
            # Never commanded this device → nothing to compare against.
            return None
        # Criterion 2: the event must be NEWER than the last command.
        if event.timestamp <= entry.sent_at:
            return None

        full_match = self._matches_ledger(event, entry)
        ledger_state = entry.state_at(now, self._staleness_timeout)
        if ledger_state in (LedgerEntryState.STALE, LedgerEntryState.EXPIRED):
            # Grenzfall: the detection pause is VALUE-BASED — only
            # events matching the stale entry (late acks) are swallowed;
            # deviating values are real user input and fall through.
            if full_match:
                return None
        elif full_match:
            return None  # criterion 1: complete echo of our own command

        # Criterion 5: partial propagation (mode-ack before temp-ack).
        if self._is_partial_echo(event, entry):
            return None

        # Criterion 3: difference explained by the calibration offset?
        calibration_explained = (
            event.new_temp is not None
            and entry.target_temperature is not None
            and _temps_match(
                event.new_temp, entry.target_temperature + calibration_offset,
            )
        )
        return OverrideDetectionResult(
            matched=not calibration_explained,
            timestamp=event.timestamp.timestamp(),
            calibration_explained=calibration_explained,
            source="ui" if event.is_user_context else "physical",
        )

    @staticmethod
    def _matches_ledger(event: TriggerEvent, entry: LedgerEntry) -> bool:
        """Full echo: both commanded dimensions confirmed (a dimension we
        never commanded — ledger None — matches anything)."""
        mode_ok = entry.hvac_mode is None or _modes_match(event.new_hvac, entry.hvac_mode)
        temp_ok = entry.target_temperature is None or _temps_match(
            event.new_temp, entry.target_temperature,
        )
        return mode_ok and temp_ok

    @staticmethod
    def _is_partial_echo(event: TriggerEvent, entry: LedgerEntry) -> bool:
        """Criterion 5: the event matches the ledger in exactly ONE
        dimension while the OTHER still holds the PREVIOUS state value —
        Zigbee2MQTT-style split acks (real V1 bug: false 8 °C override on
        the off→heat transition)."""
        mode_acks = (
            entry.hvac_mode is not None
            and _modes_match(event.new_hvac, entry.hvac_mode)
        )
        temp_acks = (
            entry.target_temperature is not None
            and event.new_temp is not None
            and _temps_match(event.new_temp, entry.target_temperature)
        )
        # A momentarily ABSENT temperature attribute (new_temp is None) is
        # not a temperature change — the device dropped the attribute mid
        # mode-transition. Treat it as "temp unchanged" so a bare mode-ack
        # is recognised as a partial echo instead of a false override.
        temp_unchanged = (
            event.new_temp is None
            or _temps_match(event.new_temp, event.old_temp, 0.01)
        )
        mode_unchanged = _modes_match(event.new_hvac, event.old_hvac)
        return (mode_acks and not temp_acks and temp_unchanged) or (
            temp_acks and not mode_acks and mode_unchanged
        )
