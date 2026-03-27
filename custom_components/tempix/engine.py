"""
Tempix – Engine (MRO-composed).

The ``TempixEngine`` is assembled from domain-specific mixins.
Each mixin lives in its own file for easier navigation and review.

    ┌─────────────────────┐
    │ CalibrationMixin    │  Valve changes, offset/Tado calibration
    │ ProtectionMixin     │  Window, frost, liming, season mode
    │ ScheduleMixin       │  Scheduler, adjustments, comfort decision
    │ CalendarMixin       │  Calendar events, tags, comfort activation
    │ PresenceMixin       │  Persons, guest, proximity, away
    │ TemperatureMixin    │  Comfort/eco/window temperature, target chain
    │ EngineBaseMixin     │  State access, parsing, rounding, logging
    └─────────────────────┘

Import path stays identical::

    from custom_components.tempix.engine import TempixEngine
"""
from __future__ import annotations

from custom_components.tempix.engine_base import EngineBaseMixin
from custom_components.tempix.engine_temperature import TemperatureMixin
from custom_components.tempix.engine_presence import PresenceMixin
from custom_components.tempix.engine_protection import ProtectionMixin
from custom_components.tempix.engine_schedule import ScheduleMixin
from custom_components.tempix.engine_calendar import CalendarMixin
from custom_components.tempix.engine_calibration import CalibrationMixin


class TempixEngine(
    CalibrationMixin,
    ProtectionMixin,
    ScheduleMixin,
    CalendarMixin,
    PresenceMixin,
    TemperatureMixin,
    EngineBaseMixin,
):
    """Stateless logic engine – composed from domain-specific mixins.

    All public methods remain unchanged. The coordinator continues to use::

        self.engine = TempixEngine(hass, config)
    """

    pass
