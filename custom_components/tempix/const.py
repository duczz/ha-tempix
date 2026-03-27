"""Constants for the Tempix integration."""
from enum import Enum
from homeassistant.const import (
    Platform,
    STATE_ON,
    STATE_OFF,
    STATE_HOME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)

DOMAIN = "tempix"
VERSION = "1.6.0"
PLATFORMS = [Platform.CLIMATE, Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH, Platform.NUMBER, Platform.SELECT]

INVALID_STATES = [STATE_UNKNOWN, STATE_UNAVAILABLE, None, ""]

# ─── Configuration Keys ──────────────────────────────────────────────────────

# 1. Thermostats & Sensors
CONF_NAME = "name"
CONF_TRVS = "trvs"
CONF_TEMPERATURE_SENSOR = "temp_sensor"
CONF_OUTSIDE_TEMP_SENSOR = "outside_temp_sensor"
CONF_OUTSIDE_TEMP_THRESHOLD = "outside_temp_threshold"
CONF_OUTSIDE_TEMP_FALLBACK = "outside_temp_fallback"
CONF_OUTSIDE_TEMP_HYSTERESIS = "outside_temp_hysteresis"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_ROOM_TEMP_THRESHOLD_ENABLED = "room_temp_threshold_enabled"
CONF_ROOM_TEMP_THRESHOLD = "room_temp_threshold"

# 2. Comfort Temperature
CONF_TEMPERATURE_COMFORT_STATIC = "temp_comfort_static"
CONF_HVAC_MODE_COMFORT = "hvac_mode_comfort"

# 3. Eco Temperature
CONF_TEMPERATURE_ECO_STATIC = "temp_eco_static"
CONF_HVAC_MODE_ECO = "hvac_mode_eco"

# 4. Scheduling
CONF_SCHEDULERS = "schedulers"
CONF_SCHEDULER_SELECTOR = "scheduler_selector"

# 5. Persons & Devices
CONF_PERSONS = "persons"
CONF_PEOPLE_ENTERING_DURATION = "people_entering_duration"
CONF_PEOPLE_LEAVING_DURATION = "people_leaving_duration"
CONF_PERSONS_FORCE_COMFORT = "persons_force_comfort"
CONF_PERSONS_FORCE_COMFORT_START = "persons_force_comfort_start"
CONF_PERSONS_FORCE_COMFORT_END = "persons_force_comfort_end"
CONF_GUEST_MODE = "guest_mode"
CONF_GUEST_MODE_SWITCH = "guest_mode_switch"

# 6. Proximity / Geo Fencing
CONF_PROXIMITY_ENTITY = "proximity_entity"
CONF_PROXIMITY_DISTANCE = "proximity_distance"
CONF_PROXIMITY_DURATION = "proximity_duration"

# 7. Presence Detection
CONF_PRESENCE_SENSOR = "presence_sensor"
CONF_SCHEDULER_PRESENCE = "scheduler_presence"
CONF_PRESENCE_REACTION_ON = "presence_reaction_on"
CONF_PRESENCE_REACTION_OFF = "presence_reaction_off"

# 8. Adjustments & Overrides
CONF_ADJUSTMENTS = "adjustments"
CONF_SYNC_ADJUSTMENTS = "sync_adjustments"
CONF_FORCE_COMFORT_SWITCH = "force_comfort_switch"
CONF_FORCE_ECO_SWITCH = "force_eco_switch"

CONF_PARTY_MODE_SWITCH = "party_mode_switch"
CONF_PARTY_TEMPERATURE = "party_temperature"


# 9. Temperature Tweaks
CONF_MIN_INSTEAD_OF_OFF = "min_instead_of_off"
CONF_RESET_TEMPERATURE = "reset_temperature"
CONF_OFF_IF_ABOVE_ROOM_TEMP = "off_if_above_room_temp"
CONF_OFF_IF_NOBODY_HOME = "off_if_nobody_home"
CONF_UI_CHANGE = "ui_change"
CONF_PHYSICAL_CHANGE = "physical_change"
CONF_HYSTERESIS = "hysteresis"

# 10. Away Mode
CONF_AWAY_OFFSET = "away_offset"
CONF_AWAY_SCHEDULER_MODE = "away_scheduler_mode"
CONF_AWAY_PRESENCE_MODE = "away_presence_mode"
CONF_AWAY_IGNORE_PEOPLE = "away_ignore_people"

# 11. Window & Door Detection
CONF_WINDOW_SENSORS = "window_sensors"
CONF_WINDOW_REACTION_OPEN = "window_reaction_open"
CONF_WINDOW_REACTION_CLOSE = "window_reaction_close"
CONF_WINDOW_OPEN_TEMP = "window_open_temp"
CONF_WINDOW_LEGACY_RESTORE = "window_legacy_restore"

# 12. Calibration
CONF_CALIBRATION_MODE = "calibration_mode"
CONF_CALIBRATION_KEYWORD = "calibration_keyword"
CONF_CALIBRATION_TIMEOUT = "calibration_timeout"
CONF_CALIBRATION_DELTA = "calibration_delta"
CONF_CALIBRATION_STEP_SIZE = "calibration_step_size"
CONF_GENERIC_CALIBRATION_LIMIT = "generic_calibration_limit"

# 13. Aggressive Mode
CONF_AGGRESSIVE_MODE_SELECTOR = "aggressive_mode_selector"
CONF_AGGRESSIVE_RANGE = "aggressive_range"
CONF_AGGRESSIVE_OFFSET = "aggressive_offset"
# Legacy switches (will be migrated)
CONF_CALIBRATION_ENABLED = "calibration_enabled"
CONF_CALIBRATION_GENERIC = "calibration_generic"
CONF_AGGRESSIVE_MODE = "aggressive_mode"
CONF_AGGRESSIVE_CALIBRATION = "aggressive_calibration"
CONF_AGGRESSIVE_CALIBRATION_SWITCH = "aggressive_calibration_switch"

# 14. Frost Protection
CONF_FROST_PROTECTION_ENABLED = "frost_protection_enabled"
CONF_FROST_PROTECTION_TEMP = "frost_protection_temp"
CONF_FROST_PROTECTION_DURATION = "frost_protection_duration"

# 15. Liming Protection
CONF_LIMING_PROTECTION = "liming_protection"
CONF_LIMING_DAY = "liming_day"
CONF_LIMING_TIME = "liming_time"
CONF_LIMING_DURATION = "liming_duration"
CONF_LIMING_IN_SEASON = "liming_in_season"

# 16. On/Off Automation Options
CONF_SEASON_MODE_ENTITY = "season_mode_entity"
CONF_IDLE_TEMPERATURE = "idle_temperature"
CONF_HYSTERESIS = "hysteresis"

# 17. Dynamic Valve Positioning
CONF_VALVE_MODE = "valve_mode"
CONF_VALVE_DIFF = "valve_diff"
CONF_VALVE_STEP = "valve_step"
CONF_VALVE_MAX = "valve_max"
CONF_VALVE_TIMEOUT = "valve_timeout"
CONF_VALVE_KEYWORD = "valve_keyword"

# 18. Custom Settings
CONF_ACTION_DELAY = "action_delay"
CONF_LOG_LEVEL = "log_level"

# 19. Automation Control
CONF_AUTOMATION_ACTIVE = "automation_active"
CONF_MANUAL_OVERRIDE_PAUSE = "manual_override_pause"
CONF_DEBUG_MODE = "debug_mode"
CONF_SENSOR_RETENTION = "sensor_retention"

# 20. Evolution Features (v1.3.0)
CONF_OPTIMUM_START = "optimum_start"
CONF_WEATHER_ANTICIPATION = "weather_anticipation"
CONF_WEATHER_OFFSET = "weather_offset"

# 21. Calendar Integration (v1.4.0)
CONF_SCHEDULING_MODE = "scheduling_mode"
SCHEDULING_MODE_HELPER = "helper"
SCHEDULING_MODE_CALENDAR = "calendar"

CONF_CALENDAR = "calendar"
CONF_CALENDAR_EVENT = "calendar_event"
CONF_CALENDAR_ROOM = "calendar_room"
CONF_CALENDAR_HVAC_MODE = "calendar_hvac_mode"
CONF_CALENDAR_COMFORT_TEMP = "calendar_comfort_temp"
CONF_CALENDAR_ECO_TEMP = "calendar_eco_temp"
CONF_CALENDAR_SCAN_INTERVAL = "calendar_scan_interval"
CONF_SYNC_CALENDAR_WITH_ENTITIES = "sync_calendar_with_entities"

# 22. Adaptive Learning (v1.5.0)
CONF_LEARNED_HEATING_RATE = "learned_heating_rate"
CONF_HEATING_RATE_LOOKBACK = "heating_rate_lookback"
CONF_MAX_OPTIMUM_START = "max_optimum_start"

# ─── Defaults ─────────────────────────────────────────────────────────────────

DEFAULT_NAME = "Tempix"
DEFAULT_COMFORT_TEMP = 22.0
DEFAULT_ECO_TEMP = 19.0
DEFAULT_FROST_TEMP = 5.0
DEFAULT_PARTY_TEMP = 18.0
DEFAULT_OUTSIDE_THRESHOLD = 15.0
DEFAULT_OUTSIDE_HYSTERESIS = 1.0
DEFAULT_ROOM_THRESHOLD = 18.0
DEFAULT_IDLE_TEMP = 0.0
DEFAULT_AWAY_OFFSET = 0.0
DEFAULT_AGGRESSIVE_RANGE = 0.3
DEFAULT_AGGRESSIVE_OFFSET = 1.0
DEFAULT_CALIBRATION_DELTA = 0.5
DEFAULT_HYSTERESIS = 0.3
DEFAULT_GENERIC_CALIBRATION_LIMIT = 5.0
DEFAULT_VALVE_DIFF = 1.0
DEFAULT_VALVE_MAX = 100
DEFAULT_VALVE_STEP = 10
DEFAULT_PROXIMITY_DISTANCE = 500
DEFAULT_PROXIMITY_DURATION = {"hours": 0, "minutes": 0, "seconds": 0}
DEFAULT_WINDOW_OPEN_TEMP = 0.0
DEFAULT_LIMING_DURATION = 1
DEFAULT_ACTION_DELAY = {"hours": 0, "minutes": 0, "seconds": 2}
DEFAULT_CALIBRATION_TIMEOUT = {"hours": 0, "minutes": 1, "seconds": 0}
DEFAULT_VALVE_TIMEOUT = {"hours": 0, "minutes": 20, "seconds": 0}
DEFAULT_SENSOR_RETENTION = {"hours": 0, "minutes": 0, "seconds": 30}
DEFAULT_CALENDAR_SCAN_INTERVAL = 15
DEFAULT_CALENDAR_COMFORT_TEMP = 21.0
DEFAULT_CALENDAR_ECO_TEMP = 19.0
DEFAULT_WEATHER_OFFSET = 1.0
DEFAULT_HEATING_RATE = 1.0  # °C per hour
DEFAULT_HEATING_RATE_LOOKBACK = 5  # cycles
DEFAULT_MAX_OPTIMUM_START = {"hours": 2, "minutes": 0, "seconds": 0}

DEFAULT_FROST_DURATION = {"days": 1, "hours": 0, "minutes": 0, "seconds": 0}
DEFAULT_WINDOW_REACTION_OPEN = {"hours": 0, "minutes": 1, "seconds": 0}
DEFAULT_WINDOW_REACTION_CLOSE = {"hours": 0, "minutes": 1, "seconds": 0}
DEFAULT_PRESENCE_REACTION_ON = {"hours": 0, "minutes": 0, "seconds": 0}
DEFAULT_PRESENCE_REACTION_OFF = {"hours": 0, "minutes": 0, "seconds": 0}
DEFAULT_PEOPLE_ENTERING = {"hours": 0, "minutes": 0, "seconds": 0}
DEFAULT_PEOPLE_LEAVING = {"hours": 0, "minutes": 0, "seconds": 0}

DEFAULT_MIN_TEMP = 5.0
DEFAULT_MAX_TEMP = 30.0
DEFAULT_ROOM_TEMP_FALLBACK = 20.0
DEFAULT_CALIBRATION_KEEPALIVE = 20  # minutes

AGGRESSIVE_MODE_OFF = "off"
AGGRESSIVE_MODE_TARGET = "target_temp"
AGGRESSIVE_MODE_CALIBRATION = "calibration"
AGGRESSIVE_MODES = [AGGRESSIVE_MODE_OFF, AGGRESSIVE_MODE_TARGET, AGGRESSIVE_MODE_CALIBRATION]

CALIBRATION_MODE_OFF = "off"
CALIBRATION_MODE_NATIVE = "native"
CALIBRATION_MODE_GENERIC = "generic"
CALIBRATION_MODES = [CALIBRATION_MODE_OFF, CALIBRATION_MODE_NATIVE, CALIBRATION_MODE_GENERIC]

CALIBRATION_STEP_SIZE_AUTO = "auto"
CALIBRATION_STEP_SIZE_FULL = "full"
CALIBRATION_STEP_SIZE_HALF = "half"
CALIBRATION_STEP_SIZE_PRECISION = "precise"
CALIBRATION_STEP_SIZE_OPTIONS = [
    CALIBRATION_STEP_SIZE_AUTO,
    CALIBRATION_STEP_SIZE_FULL,
    CALIBRATION_STEP_SIZE_HALF,
    CALIBRATION_STEP_SIZE_PRECISION,
]

TADO_MIN_OFFSET = -10.9
TADO_MAX_OFFSET = 10.9

# ─── Heating State Enum ───────────────────────────────────────────────────────

class HeatingState(Enum):
    """Explicit heating state – formalises the implicit priority chain.

    Priority order (highest wins):
        MANUAL_OVERRIDE > PAUSED > INACTIVE > FROST_PROTECTION > WINDOW_OPEN >
        LIMING > PARTY > FORCE_COMFORT > FORCE_ECO > ADJUSTMENT >
        SMART_PREHEATING > AWAY > COMFORT > ECO
    """
    MANUAL_OVERRIDE = "manual_override"
    PAUSED = "paused"
    INACTIVE = "inactive"
    FROST_PROTECTION = "frost_protection"
    WINDOW_OPEN = "window_open"
    LIMING = "liming"
    PARTY = "party"
    FORCE_COMFORT = "force_comfort"
    FORCE_ECO = "force_eco"
    ADJUSTMENT = "adjustment"
    SMART_PREHEATING = "smart_preheating"
    AWAY = "away"
    COMFORT = "comfort"
    ECO = "eco"
