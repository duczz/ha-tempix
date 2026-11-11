"""V2Bridge — named adapter isolating every direct V1-TempixEngine access
used by the V2 pipeline (``ParallelValidator``).

This collects the V1-engine members the V2 pipeline touches behind one
named surface, so a future V1 deletion has a single place to replace. Pure
restructuring — no behavior change: every member is a 1:1 attribute
rebinding or property forward onto the wrapped V1 engine, not a
reimplementation. Vorbild: the ``EngineClock`` Protocol in ``clock.py``.

Two members carry documented side effects on the underlying V1 engine,
preserved here rather than hidden: ``is_anybody_home`` writes
``engine._last_home_status``; ``get_uncertainty_reasons`` is explicitly
side-effect-free (the contrast is called out at the
``get_uncertainty_reasons`` call site in ``parallel_validation.py``).
"""
from __future__ import annotations

from typing import Any


class V2Bridge:
    """Adapter over a V1 ``TempixEngine`` exposing exactly the members the
    V2 pipeline needs, under public names (no leading underscore) even
    where V1's own method is internal."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self.get_state = engine._get_state
        self.parse_dt = engine._parse_dt
        self.state_value = engine._state_value
        self.float_state = engine._float_state
        self.entity_is_missing = engine._entity_is_missing
        self.resolve_window_open_temperature = engine.resolve_window_open_temperature
        self.is_away = engine.is_away
        self.is_force_comfort_temp = engine.is_force_comfort_temp
        self.is_force_eco_temp = engine.is_force_eco_temp
        # Side effect: writes engine._last_home_status. Preserved, not hidden.
        self.is_anybody_home = engine.is_anybody_home
        self.resolve_comfort_temperature = engine.resolve_comfort_temperature
        self.resolve_room_temperature = engine._resolve_room_temp
        self.check_proximity_arrived = engine.check_proximity_arrived
        self.check_proximity_towards = engine.check_proximity_towards
        self.resolve_outside_temperature = engine._resolve_outside_temp
        # Side-effect-free (unlike is_anybody_home).
        self.get_uncertainty_reasons = engine.get_uncertainty_reasons
        self.is_automation_active = engine.is_automation_active
        self.is_window_open = engine.is_window_open
        self.is_liming_time = engine.is_liming_time
        self.resolve_eco_temperature = engine.resolve_eco_temperature
        self.is_sunshine_offset_active = engine.is_sunshine_offset_active
        self.calculate_valve_position = engine.calculate_valve_position
        self.calculate_calibration = engine.calculate_calibration
        self.get_active_scheduler = engine.get_active_scheduler

    @property
    def is_cooling(self) -> bool:
        return self._engine.is_cooling

    @property
    def calendar_events(self) -> dict:
        return self._engine._calendar_events

    @property
    def schedule_slots(self) -> dict:
        return self._engine._schedule_slots


class EngineSnapshotStates:
    """states-view over a ``V2Bridge``.

    No timing artifacts: both engines must see identical state
    values. V1's ``get_state`` prefers the per-cycle snapshot dict (fallback:
    live) — routing the builder and the capability map through it guarantees
    byte-identical inputs. Deliberately exposes NO ``services`` attribute:
    any accidental service call in the dry-run would crash immediately
    instead of touching hardware.
    """

    def __init__(self, bridge: V2Bridge) -> None:
        self._bridge = bridge
        self.states = self

    def get(self, entity_id: str) -> Any:
        return self._bridge.get_state(entity_id)
