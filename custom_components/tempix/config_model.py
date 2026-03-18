"""Typed configuration model for Tempix.

All configuration values are parsed exactly once at construction time.
Duration dicts become ``timedelta``, numeric strings become ``float``,
entity references become ``list[str]``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, fields
from datetime import timedelta
from typing import Any

_LOGGER = logging.getLogger(__name__)


# ── standalone helpers ────────────────────────────────────────────────────────


def parse_duration(value: Any, default_seconds: int = 0) -> timedelta:
    """Parse a duration value (dict / int / float / timedelta) to ``timedelta``."""
    if value is None:
        return timedelta(seconds=default_seconds)
    if isinstance(value, timedelta):
        return value
    if isinstance(value, dict):
        try:
            return timedelta(**value)
        except TypeError:
            return timedelta(seconds=default_seconds)
    if isinstance(value, (int, float)):
        return timedelta(seconds=int(value))
    return timedelta(seconds=default_seconds)


def _parse_entity_list(value: Any) -> list[str]:
    """Normalise entity references to a flat ``list[str]``."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if isinstance(value, str):
        return [value]
    return []


def _parse_adjustments(value: Any) -> list[dict]:
    """Parse adjustments from JSON string or list."""
    if not value or value == "[]":
        return []
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(value, list):
        return value
    return []


# ── dataclass ─────────────────────────────────────────────────────────────────


@dataclass
class TempixConfig:
    """Typed, pre-parsed configuration for Tempix.

    Create via ``TempixConfig.from_dict(raw_dict)`` –
    all parsing happens inside the factory, never at the call-site.
    """

    # ── Identity ─────────────────────────────────────────────────────────
    name: str = "Tempix"

    # ── Thermostats & Sensors ────────────────────────────────────────────
    trvs: list[str] = field(default_factory=list)
    temp_sensor: str | list | None = None
    outside_temp_sensor: str | None = None
    outside_temp_threshold: float = 15.0
    outside_temp_hysteresis: float = 1.0
    outside_temp_fallback: bool = False
    weather_entity: str | None = None
    room_temp_threshold_enabled: bool = False
    room_temp_threshold: float = 18.0

    # ── Comfort / Eco ────────────────────────────────────────────────────
    temp_comfort_static: float = 22.0
    hvac_mode_comfort: str = "heat"
    temp_eco_static: float = 19.0
    hvac_mode_eco: str = "heat"

    # ── Scheduling ───────────────────────────────────────────────────────
    schedulers: list[str] = field(default_factory=list)
    scheduler_selector: str | None = None
    scheduling_mode: str = "helper"

    # ── Persons & Guest ──────────────────────────────────────────────────
    persons: list[str] = field(default_factory=list)
    people_entering_duration: timedelta = field(default_factory=timedelta)
    people_leaving_duration: timedelta = field(default_factory=timedelta)
    persons_force_comfort: bool = False
    persons_force_comfort_start: str = "07:00:00"
    persons_force_comfort_end: str = "22:00:00"
    guest_mode: list[str] = field(default_factory=list)
    guest_mode_switch: bool = False

    # ── Proximity ────────────────────────────────────────────────────────
    proximity_entity: str | None = None
    proximity_distance: int = 500
    proximity_duration: timedelta = field(default_factory=timedelta)

    # ── Presence ─────────────────────────────────────────────────────────
    presence_sensor: str | list | None = None
    scheduler_presence: str | None = None
    presence_reaction_on: timedelta = field(default_factory=timedelta)
    presence_reaction_off: timedelta = field(default_factory=timedelta)

    # ── Adjustments & Overrides ──────────────────────────────────────────
    adjustments: list[dict] = field(default_factory=list)
    sync_adjustments: bool = False
    force_comfort_switch: bool = False
    force_eco_switch: bool = False
    party_mode_switch: bool = False
    party_temperature: float | None = None

    # ── Temperature Tweaks ───────────────────────────────────────────────
    min_instead_of_off: bool = False
    reset_temperature: bool = False
    off_if_above_room_temp: bool = False
    off_if_nobody_home: bool = False
    ui_change: bool = False
    physical_change: bool = False
    hysteresis: float = 0.3

    # ── Away ─────────────────────────────────────────────────────────────
    away_offset: float = 0.0
    away_scheduler_mode: bool = False
    away_presence_mode: bool = False
    away_ignore_people: bool = False

    # ── Window ───────────────────────────────────────────────────────────
    window_sensors: list[str] = field(default_factory=list)
    window_reaction_open: timedelta = field(
        default_factory=lambda: timedelta(minutes=1)
    )
    window_reaction_close: timedelta = field(
        default_factory=lambda: timedelta(minutes=1)
    )
    window_open_temp: float = 0.0
    window_legacy_restore: bool = False

    # ── Calibration ──────────────────────────────────────────────────────
    calibration_mode: str = "off"
    calibration_keyword: str = "calibration"
    calibration_timeout: timedelta = field(
        default_factory=lambda: timedelta(minutes=1)
    )
    calibration_delta: float = 0.5
    calibration_step_size: str = "auto"
    generic_calibration_limit: float = 5.0

    # ── Aggressive Mode ──────────────────────────────────────────────────
    aggressive_mode_selector: str = "off"
    aggressive_range: float = 0.3
    aggressive_offset: float = 1.0

    # ── Frost Protection ─────────────────────────────────────────────────
    frost_protection_enabled: bool = False
    frost_protection_temp: float = 5.0
    frost_protection_duration: timedelta = field(
        default_factory=lambda: timedelta(days=1)
    )

    # ── Liming Protection ────────────────────────────────────────────────
    liming_protection: bool = False
    liming_day: str = "mon"
    liming_time: str = "12:00:00"
    liming_duration: int = 1
    liming_in_season: bool = False

    # ── Season / Automation ──────────────────────────────────────────────
    season_mode_entity: str | None = None
    automation_active: bool = True
    manual_override_pause: bool = False
    idle_temperature: float = 0.0

    # ── Valve Positioning ────────────────────────────────────────────────
    valve_mode: str = "off"
    valve_diff: float = 1.0
    valve_step: int = 10
    valve_max: int = 100
    valve_timeout: timedelta = field(
        default_factory=lambda: timedelta(minutes=20)
    )
    valve_keyword: str = "valve"

    # ── Custom Settings ──────────────────────────────────────────────────
    action_delay: timedelta = field(
        default_factory=lambda: timedelta(seconds=2)
    )
    log_level: str = "info"
    debug_mode: bool = False
    sensor_retention: timedelta = field(
        default_factory=lambda: timedelta(seconds=30)
    )

    # ── Optimum Start / Evolution ────────────────────────────────────────
    optimum_start: bool = False
    weather_anticipation: bool = False
    weather_offset: float = 1.0
    learned_heating_rate: float = 1.0
    heating_rate_lookback: int = 5
    max_optimum_start: timedelta = field(
        default_factory=lambda: timedelta(hours=2)
    )

    # ── Calendar ─────────────────────────────────────────────────────────
    calendar: list[str] = field(default_factory=list)
    calendar_event: str = ""
    calendar_room: str = ""
    calendar_hvac_mode: str | None = None
    calendar_comfort_temp: float = 21.0
    calendar_eco_temp: float = 19.0
    calendar_scan_interval: int = 15
    sync_calendar_with_entities: bool = False

    # ── Internal (not a config key – stores raw dict for reload comparison) ──
    _raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # ── factory ───────────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TempixConfig:
        """Create a typed config from a raw ``entry.data + entry.options`` dict.

        All duration dicts → ``timedelta``, all floats coerced, entity lists
        normalised.  Unknown keys are silently ignored.
        """
        g = raw.get

        party_temp = g("party_temperature")

        return cls(
            # Identity
            name=g("name", "Tempix"),
            # Thermostats & Sensors
            trvs=_parse_entity_list(g("trvs")),
            temp_sensor=g("temp_sensor"),
            outside_temp_sensor=g("outside_temp_sensor"),
            outside_temp_threshold=float(g("outside_temp_threshold", 15.0)),
            outside_temp_hysteresis=float(g("outside_temp_hysteresis", 1.0)),
            outside_temp_fallback=bool(g("outside_temp_fallback", False)),
            room_temp_threshold_enabled=bool(g("room_temp_threshold_enabled", False)),
            room_temp_threshold=float(g("room_temp_threshold", 18.0)),
            # Comfort / Eco
            temp_comfort_static=float(g("temp_comfort_static", 22.0)),
            hvac_mode_comfort=g("hvac_mode_comfort", "heat"),
            temp_eco_static=float(g("temp_eco_static", 19.0)),
            hvac_mode_eco=g("hvac_mode_eco", "heat"),
            # Scheduling
            schedulers=_parse_entity_list(g("schedulers")),
            scheduler_selector=g("scheduler_selector"),
            scheduling_mode=g("scheduling_mode", "helper"),
            # Persons & Guest
            persons=_parse_entity_list(g("persons")),
            people_entering_duration=parse_duration(g("people_entering_duration")),
            people_leaving_duration=parse_duration(g("people_leaving_duration")),
            persons_force_comfort=bool(g("persons_force_comfort", False)),
            persons_force_comfort_start=g("persons_force_comfort_start", "07:00:00"),
            persons_force_comfort_end=g("persons_force_comfort_end", "22:00:00"),
            guest_mode=_parse_entity_list(g("guest_mode")),
            guest_mode_switch=bool(g("guest_mode_switch", False)),
            # Proximity
            proximity_entity=g("proximity_entity"),
            proximity_distance=int(g("proximity_distance", 500)),
            proximity_duration=parse_duration(g("proximity_duration")),
            # Presence
            presence_sensor=g("presence_sensor"),
            scheduler_presence=g("scheduler_presence"),
            presence_reaction_on=parse_duration(g("presence_reaction_on")),
            presence_reaction_off=parse_duration(g("presence_reaction_off")),
            # Adjustments
            adjustments=_parse_adjustments(g("adjustments")),
            sync_adjustments=bool(g("sync_adjustments", False)),
            force_comfort_switch=bool(g("force_comfort_switch", False)),
            force_eco_switch=bool(g("force_eco_switch", False)),
            party_mode_switch=bool(g("party_mode_switch", False)),
            party_temperature=(
                float(party_temp) if party_temp is not None else None
            ),
            # Temperature Tweaks
            min_instead_of_off=bool(g("min_instead_of_off", False)),
            reset_temperature=bool(g("reset_temperature", False)),
            off_if_above_room_temp=bool(g("off_if_above_room_temp", False)),
            off_if_nobody_home=bool(g("off_if_nobody_home", False)),
            ui_change=bool(g("ui_change", False)),
            physical_change=bool(g("physical_change", False)),
            hysteresis=float(g("hysteresis", 0.3)),
            # Away
            away_offset=float(g("away_offset", 0.0)),
            away_scheduler_mode=bool(g("away_scheduler_mode", False)),
            away_presence_mode=bool(g("away_presence_mode", False)),
            away_ignore_people=bool(g("away_ignore_people", False)),
            # Window
            window_sensors=_parse_entity_list(g("window_sensors")),
            window_reaction_open=parse_duration(
                g("window_reaction_open", {"hours": 0, "minutes": 1, "seconds": 0})
            ),
            window_reaction_close=parse_duration(
                g("window_reaction_close", {"hours": 0, "minutes": 1, "seconds": 0})
            ),
            window_open_temp=float(g("window_open_temp", 0.0)),
            window_legacy_restore=bool(g("window_legacy_restore", False)),
            # Calibration
            calibration_mode=g("calibration_mode", "off"),
            calibration_keyword=g("calibration_keyword", "calibration"),
            calibration_timeout=parse_duration(
                g("calibration_timeout", {"hours": 0, "minutes": 1, "seconds": 0})
            ),
            calibration_delta=float(g("calibration_delta", 0.5)),
            calibration_step_size=g("calibration_step_size", "auto"),
            generic_calibration_limit=float(g("generic_calibration_limit", 5.0)),
            # Aggressive
            aggressive_mode_selector=g("aggressive_mode_selector", "off"),
            aggressive_range=float(g("aggressive_range", 0.3)),
            aggressive_offset=float(g("aggressive_offset", 1.0)),
            # Frost
            frost_protection_enabled=bool(g("frost_protection_enabled", False)),
            frost_protection_temp=float(g("frost_protection_temp", 5.0)),
            frost_protection_duration=parse_duration(
                g("frost_protection_duration", {"days": 1})
            ),
            # Liming
            liming_protection=bool(g("liming_protection", False)),
            liming_day=g("liming_day", "mon"),
            liming_time=g("liming_time", "12:00:00"),
            liming_duration=int(g("liming_duration", 1)),
            liming_in_season=bool(g("liming_in_season", False)),
            # Season / Automation
            season_mode_entity=g("season_mode_entity"),
            automation_active=bool(g("automation_active", True)),
            manual_override_pause=bool(g("manual_override_pause", False)),
            idle_temperature=float(g("idle_temperature", 0.0)),
            # Valve
            valve_mode=g("valve_mode", "off"),
            valve_diff=float(g("valve_diff", 1.0) or 1.0),
            valve_step=int(g("valve_step", 10) or 10),
            valve_max=int(g("valve_max", 100) or 100),
            valve_timeout=parse_duration(
                g("valve_timeout", {"hours": 0, "minutes": 20, "seconds": 0})
            ),
            valve_keyword=g("valve_keyword", "valve"),
            # Custom
            action_delay=parse_duration(
                g("action_delay", {"hours": 0, "minutes": 0, "seconds": 2})
            ),
            log_level=g("log_level", "info"),
            debug_mode=bool(g("debug_mode", False)),
            sensor_retention=parse_duration(
                g("sensor_retention", {"hours": 0, "minutes": 0, "seconds": 30})
            ),
            # Optimum Start
            optimum_start=bool(g("optimum_start", False)),
            weather_entity=g("weather_entity"),
            weather_anticipation=bool(g("weather_anticipation", False)),
            weather_offset=float(g("weather_offset", 1.0)),
            learned_heating_rate=float(g("learned_heating_rate", 1.0)),
            heating_rate_lookback=int(g("heating_rate_lookback", 5)),
            max_optimum_start=parse_duration(
                g("max_optimum_start", {"hours": 2, "minutes": 0, "seconds": 0})
            ),
            # Calendar
            calendar=_parse_entity_list(g("calendar")),
            calendar_event=g("calendar_event", ""),
            calendar_room=g("calendar_room", ""),
            calendar_hvac_mode=g("calendar_hvac_mode"),
            calendar_comfort_temp=float(g("calendar_comfort_temp", 21.0)),
            calendar_eco_temp=float(g("calendar_eco_temp", 19.0)),
            calendar_scan_interval=int(g("calendar_scan_interval", 15)),
            sync_calendar_with_entities=bool(
                g("sync_calendar_with_entities", False)
            ),
            # Internal
            _raw=dict(raw),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (for diagnostics / comparison)."""
        result: dict[str, Any] = {}
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            val = getattr(self, f.name)
            if isinstance(val, timedelta):
                total = int(val.total_seconds())
                result[f.name] = {
                    "hours": total // 3600,
                    "minutes": (total % 3600) // 60,
                    "seconds": total % 60,
                }
            else:
                result[f.name] = val
        return result
