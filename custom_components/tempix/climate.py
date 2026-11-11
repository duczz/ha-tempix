"""Tempix – Virtual Climate entity."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, UTC
from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.tempix.models.target_state import (
    TEMPERATURELESS_HVAC_MODES,
)
from custom_components.tempix.const import (
    DOMAIN,
    INVALID_STATES,
    DEFAULT_MIN_TEMP,
    DEFAULT_MAX_TEMP,
)
from custom_components.tempix.device import tempix_device_info
from custom_components.tempix.service_send import safe_service_call

_LOGGER = logging.getLogger(__name__)

# Sendeparameter fuer den Passthrough — bewusst NICHT die des
# Hintergrundzyklus. Begruendung: `_passthrough_send`.
PASSTHROUGH_TIMEOUT_S = 10
PASSTHROUGH_RETRIES = 0

# Modi, die als Comfort-/Eco-Modus im Zeitplan stehen dürfen.
#
# Entspricht `hvac_modes.SCHEDULE_HVAC_MODE_ORDER` **ohne `"off"`**: der Config-Flow führt
# `off` mit, weil ein Raum legitim „Eco = aus" haben darf (reale
# Klimaanlagen-Konfigurationen). Hier ist `off` überflüssig,
# weil `hvac_modes` es ohnehin unbedingt aufnimmt.
#
# Ändert sich die Liste in `hvac_modes.py`, muss sie hier mitgeändert werden — aber
# als bewusster Abgleich, nicht per Copy-Paste: die beiden Listen sind
# absichtlich nicht identisch.
_SCHEDULABLE_HVAC_MODES = frozenset({"heat", "cool", "heat_cool", "auto"})

# Modi, die auf der Karte AUSWÄHLBAR sind: alles, was HA überhaupt kennt.
# Bewusst NICHT von Hand gepflegt — eine eigene Liste wäre nur eine Kopie des
# Enums, die beim nächsten HA-Release still veraltet. Der Filter bleibt trotzdem
# nötig: ein Gerät darf in `hvac_modes` beliebige Strings melden, und
# `HVACMode(x)` würde darauf werfen. Die Frage ist also nicht „steht es auf
# unserer Liste", sondern „kennt Home Assistant diesen Modus".
#
# Mehr als `_SCHEDULABLE_HVAC_MODES`: `dry`/`fan_only` sind als spontaner
# Eingriff sinnvoll (Temporary Override, endet beim nächsten Zeitplan-Wechsel),
# als dauerhafter Zeitplan-Modus dagegen nicht — dafür müsste die alte Engine
# sie kennen, und sie kennt sie nirgends.
#
# Kommt in HA ein Modus dazu, ist er automatisch wählbar. Ist er temperaturlos,
# gehört er zusätzlich in `TEMPERATURELESS_HVAC_MODES` — sonst behandelt ihn der
# Rest der Kette als temperaturgeführt.
_SELECTABLE_HVAC_MODES = frozenset(m.value for m in HVACMode)

# Anzeigereihenfolge auf der Karte (HA übernimmt die Listenreihenfolge 1:1) —
# die Definitionsreihenfolge des Enums, damit auch sie automatisch mitwächst.
_HVAC_MODE_ORDER = tuple(m.value for m in HVACMode)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TempixClimate(data["coordinator"], entry)])


class TempixClimate(ClimateEntity, RestoreEntity):
    """Virtual climate wrapper presenting the Tempix computed state."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_climate"
        self._attr_name = None

        # Device Info for UI grouping
        self._attr_device_info = tempix_device_info(self._entry)

        # hvac_modes ist eine Property (siehe unten) — sie muss den Geraeten
        # folgen, die beim Setup noch unavailable sein koennen.
        # TURN_OFF/TURN_ON are mandatory (HA 2024.2+) once OFF is in hvac_modes
        # and set_hvac_mode is implemented — without them HA logs a deprecation
        # warning that will become an error in future releases.
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
        )

        # Einheit einmal beim Anlegen festschreiben, wie zuvor. Bewusst KEIN
        # `_attr_temperature_unit` / `_attr_native_temperature_unit`: HA 2026.11
        # meldet das Schreiben des alten Namens als Deprecation. Die Property
        # `native_temperature_unit` liefert diesen Wert.
        self._unit = coordinator.hass.config.units.temperature_unit
        # War zuvor eine fuenfte, unabhaengig getippte Kopie dieser
        # beiden Grenzen (bei max_temp 32.0 statt 30.0) — jeder konfigurierte
        # Sollwert, die Kalender-Clamps und plan()s eigene Kappung liegen bei
        # 30.0. Eine Karteneingabe von 31.5 wurde als current_temperature
        # publiziert und angezeigt, waehrend plan() auf 30.0 klemmte — Karte
        # und Hardware liefen bis zu 2 K auseinander, ohne jedes Signal.
        self._attr_min_temp = DEFAULT_MIN_TEMP
        self._attr_max_temp = DEFAULT_MAX_TEMP
        self._attr_target_temperature_step = 0.5

        # Restore placeholders (filled in async_added_to_hass)
        self._restored_hvac_mode: HVACMode | None = None
        self._restored_target_temp: float | None = None
        self._restored_current_temp: float | None = None

        # Optimistic UI State for Passthrough Mode
        self._optimistic_hvac_mode: HVACMode | None = None
        self._optimistic_target_temp: float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore last known state on startup to avoid 'Unknown' flash."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            try:
                self._restored_hvac_mode = HVACMode(last_state.state)
            except ValueError:
                pass
            if ATTR_TEMPERATURE in last_state.attributes:
                try:
                    self._restored_target_temp = float(last_state.attributes[ATTR_TEMPERATURE])
                except (ValueError, TypeError):
                    pass
            if "current_temperature" in last_state.attributes:
                try:
                    self._restored_current_temp = float(last_state.attributes["current_temperature"])
                except (ValueError, TypeError):
                    pass
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Outside the pause/passthrough mode the coordinator IS the
        # authority — drop optimistic values on every update. Keeping them
        # until equality left the UI showing a stale wish forever when the
        # engine overruled it (frost clamp) or after the pause ended. This
        # also produces the "constraint bouncing": the user briefly
        # sees their input, then the engine's correction becomes visible.
        # DURING the pause the coordinator values are computed-but-ignored —
        # keep the optimistic state, it mirrors what the user last sent to
        # the hardware; it gets dropped on the first update after the pause.
        if not getattr(self._coordinator.config, "manual_mode", False):
            self._optimistic_target_temp = None
            self._optimistic_hvac_mode = None

        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._coordinator._updates_enabled

    def _paused_device_state(self):
        """Waehrend der Pause: Zustand des ersten VERFUEGBAREN Geraets im Raum.

        Ausserhalb der Pause ``None`` — dort ist der Coordinator die Autoritaet
        und seine Rechnung IST das, was gefahren wird.

        **Warum das noetig ist.** In der Pause fasst
        Tempix die Hardware nicht an, rechnet aber weiter — und die Karte zeigte
        bisher diese Rechnung. Bei einem Raum mit ``hvac_mode_eco = "off"``
        heisst das: die Karte sagt „aus", waehrend die Klimaanlage laeuft.
        Aufgefallen nach einem Neustart, weil der optimistische Zustand
        (in-memory) dabei verloren geht und die Anzeige auf die Rechnung
        zurueckfiel. Genau die Information, wegen der man in die Pause geht
        („ich steuere selbst"), war damit falsch — und ein laufendes Geraet
        unsichtbar.

        `_restored_hvac_mode` half nicht: es greift erst, wenn
        ``current_hvac`` leer ist, und ``"off"`` ist ein gueltiger Wert.

        Erstes VERFUEGBARES Geraet, nicht einfach das erste: ein
        ``unavailable``-TRV wuerde sonst die Anzeige des ganzen Raums kippen.
        Bei mehreren Geraeten ist das eine Naeherung — die Karte ist in der
        Pause eine Fernbedienung fuer den Raum, kein Aggregat.
        """
        if not getattr(self._coordinator.config, "manual_mode", False):
            return None
        for trv in self._coordinator.config.trvs:
            state = self.hass.states.get(trv) if self.hass else None
            if state is not None and state.state not in INVALID_STATES:
                return state
        return None

    def _needs_target_fallback(self, hvac_mode: HVACMode) -> bool:
        """Braucht dieser Moduswechsel einen Ersatz-Sollwert?

        Nur fuer temperaturgefuehrte Modi ohne bekanntes Ziel — sonst blendet HA
        den Regler dauerhaft aus (UI-Deadlock). `off` hatte diese Ausnahme immer;
        `dry`/`fan_only` gehoeren dazu, seit sie auf der Karte
        waehlbar sind: dort ist die Abwesenheit eines Sollwerts korrekt, kein
        Deadlock.
        """
        if hvac_mode == HVACMode.OFF:
            return False
        if str(hvac_mode) in TEMPERATURELESS_HVAC_MODES:
            return False
        return self.target_temperature is None

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Modi, die auf der Tempix-Karte wählbar sind.

        Zwei Quellen, bewusst vereinigt:

        * die **konfigurierten** Comfort-/Eco-Modi — die müssen immer wählbar
          sein, auch wenn ein Gerät beim Start noch ``unavailable`` ist und
          seine Fähigkeiten nicht meldet;
        * alles, was die **Geräte des Raums** tatsächlich können. Damit landen
          ``dry`` und ``fan_only`` auf der Karte, sobald ein Gerät sie meldet.

        Warum die Geräte-Modi überhaupt dazukommen: Sie im Zeitplan zu
        *konfigurieren* ist eine andere Frage (dafür filtert der Config-Flow
        weiterhin) — hier geht es um den spontanen Griff zur Karte („heute mal
        nur lüften"). Das erzeugt einen Temporary Override, der beim nächsten
        Zeitplan-Wechsel von selbst endet.

        Bewusst eine Property statt ``_attr_hvac_modes``: beim Setup nach einem
        Neustart sind die TRVs oft noch nicht da. Ein einmalig im Konstruktor
        berechnetes Attribut bliebe dann dauerhaft zu kurz.

        Union statt Schnittmenge bei mehreren Geräten: kann ein Gerät den Modus
        nicht, greift der typisierte Fallback des Dispatchers (z. B. ``dry`` →
        ``off``) — eine Schnittmenge würde dagegen in gemischten Räumen alles
        Interessante wegschneiden.
        """
        modes: set[str] = {HVACMode.OFF.value}

        for attr in ("hvac_mode_comfort", "hvac_mode_eco"):
            m = getattr(self._coordinator.config, attr, None)
            if m in _SCHEDULABLE_HVAC_MODES:
                modes.add(m)

        # `self.hass` kann vor `async_added_to_hass` None sein — gleich
        # abgesichert wie `_paused_device_state`.
        for trv in self._coordinator.config.trvs:
            state = self.hass.states.get(trv) if self.hass else None
            if not state:
                continue
            for m in state.attributes.get("hvac_modes") or []:
                if m in _SELECTABLE_HVAC_MODES:
                    modes.add(m)

        return [HVACMode(m) for m in _HVAC_MODE_ORDER if m in modes]

    @property
    def hvac_mode(self) -> HVACMode:
        if self._optimistic_hvac_mode is not None:
            return self._optimistic_hvac_mode

        # In der Pause spiegelt die Karte das GERAET, nicht die Rechnung.
        paused = self._paused_device_state()
        if paused is not None:
            try:
                return HVACMode(paused.state)
            except ValueError:
                pass

        m = self._coordinator.current_hvac
        if m:
            try:
                mode = HVACMode(m)
            except ValueError:
                pass
            else:
                # AU-1: Restore nur bis zum ersten gültigen Wert.
                self._restored_hvac_mode = None
                return mode
        if self._restored_hvac_mode is not None:
            return self._restored_hvac_mode
        return HVACMode.OFF

    # HA 2026.11 hat die Temperatur-Properties mit `native_`-Praefix versehen und
    # warnt bei den alten Namen (Entfernung in HA 2027.11). HA < 2026.11 kennt nur
    # die alten Namen. Deshalb stehen beide Namen in DIESER Klasse: die `native_*`
    # tragen die Logik, die alten sind duenne Aliase. Beides auf Klassenebene —
    # HA prueft `vars(klass)`, ein Setzen im __init__ zaehlt nicht.
    # Aliase entfallen, sobald hacs.json auf >= 2026.11.0 steht. Dann auch
    # `_needs_target_fallback` (liest `self.target_temperature`) und die Tests
    # anpassen, die `target_temperature` patchen bzw. auf einem MagicMock setzen
    # (test_climate_hvac_modes, test_c3_passthrough_send, test_a3_window_override_ui_fixes).
    @property
    def native_temperature_unit(self) -> str:
        """Einheit der Tempix-Werte: die Systemeinheit von HA beim Anlegen.

        Bei unveraendertem Einheitensystem aendert die Umrechnung keinen Wert.
        Beim Anlegen festgeschrieben (``self._unit``), nicht live gelesen — wie
        bisher. Nach einem Wechsel des Einheitensystems meldet die Entity
        weiter die alte Einheit; HA rechnet die Anzeige dann um.
        """
        return self._unit

    @property
    def temperature_unit(self) -> str:
        """Alias fuer HA < 2026.11."""
        return self.native_temperature_unit

    @property
    def target_temperature(self) -> float | None:
        """Alias fuer HA < 2026.11."""
        return self.native_target_temperature

    @property
    def current_temperature(self) -> float | None:
        """Alias fuer HA < 2026.11."""
        return self.native_current_temperature

    @property
    def native_target_temperature(self) -> float | None:
        """Return the target temperature, falling back to restored value."""
        if self._optimistic_target_temp is not None:
            return self._optimistic_target_temp

        # Pause: Geraetewert statt Rechnung, siehe `_paused_device_state`.
        paused = self._paused_device_state()
        if paused is not None:
            device_temp = paused.attributes.get("temperature")
            if device_temp is not None:
                try:
                    return float(device_temp)
                except (TypeError, ValueError):
                    pass

        if self._coordinator.current_temperature is not None:
            # AU-1: Der Restore-Wert überbrückt nur die Zeit bis zum ersten
            # gültigen Wert nach dem Start. Danach wäre er ein Wert von vor dem
            # Neustart, der nach Quellenverlust als aktuell erschiene.
            self._restored_target_temp = None
            return self._coordinator.current_temperature
        return self._restored_target_temp

    @property
    def native_current_temperature(self) -> float | None:
        """Return the actual room temperature, falling back to restored value.

        Liest snapshot.room_temperature statt live
        engine._resolve_room_temp() — nicht room_temperature_resolved, das
        ist die Builder-eigene Fallback-Resolution, exklusiv für
        die Frostschutz-Pfade (models/snapshot.py-Docstring).
        """
        snapshot = self._coordinator._v2_last_snapshot
        temp = snapshot.room_temperature if snapshot is not None else None
        if temp is not None:
            # AU-1: Restore nur bis zum ersten gültigen Wert (siehe oben).
            self._restored_current_temp = None
            return temp
        return self._restored_current_temp

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cfg = self._coordinator.config
        engine = self._coordinator.engine
        # automation_active/is_away/is_party/next_schedule_transition lesen
        # den Snapshot statt live engine.*() — outside_temp_ok
        # (zustandsbehaftete Hysterese, kein nativer Resolver) bleibt
        # V1-gebrückt. season_mode ebenfalls Snapshot, Default True bei
        # fehlendem Snapshot (V1s Vertrag). Kein Live-Fallback, wenn noch kein
        # V2-Zyklus lief (konsistent mit binary_sensor.py).
        snapshot = self._coordinator._v2_last_snapshot

        status = self._coordinator.current_reason
        if engine.config.manual_mode:
            status = "Paused (Passthrough Mode Active)"

        return {
            # Statische Config-Liste der gesteuerten Original-Climates (TRVs, Klimaanlagen) (ohne
            # Self-Referenz, wie in _passthrough_send). Anders als valve_targets
            # (plan-abgeleitet, kann leer sein) immer vollständig.
            "source_climates": [t for t in cfg.trvs if t != self.entity_id],
            "reason": status,
            "target_temperature_calculated": self._coordinator.current_temperature,
            "hvac_mode_calculated": self._coordinator.current_hvac,
            # This is the computed gate (season AND outside AND the
            # switch below), NOT the automation_active switch's raw value —
            # can read False while the switch is on. Not renamed, see
            # engine_protection.is_automation_active docstring.
            "automation_active": bool(snapshot and snapshot.automation_active),
            "manual_mode": engine.config.manual_mode,
            "season_mode": snapshot.season_mode if snapshot else True,
            "is_away": bool(snapshot and snapshot.away),
            "is_party": bool(snapshot and snapshot.party_active),
            "outside_temp_ok": engine.check_outside_threshold(),
            # Die präsenzbasierte Absenkung existierte nur in V1, hatte in V2
            # nie ein Goal und wurde entfernt. Das Attribut bleibt für
            # Dashboard-Templates erhalten, meldet aber dauerhaft False — der
            # WIRKSAME Frostschutz ist die Untergrenze (FrostConstraint) und
            # zeigt sich im Status als `frost_protection`, nicht hier.
            "frost_protection_active": False,
            "calibration_offsets": self._coordinator.last_calibrations,
            "valve_targets": {
                c["entity_id"]: c["temperature"] for c in self._coordinator.last_changes if "temperature" in c
            },
            "next_schedule_transition": snapshot.next_schedule_transition if snapshot else None,
        }



    async def _passthrough_send(self, service: str, service_data: dict[str, Any]) -> None:
        """Reiche EINE Karten-Eingabe waehrend der Pause an alle Hardware-TRVs.

        Frueher stand hier `async_call(blocking=False)` plus
        ein `gather`-Exception-Check. Fire-and-forget plant den Aufruf nur ein und
        kehrt sofort zurueck — der Check fing damit ausschliesslich Fehler VOR dem
        Einplanen (unbekannter Service, kaputtes Schema), nie ein Geraet, das
        nicht antwortet. Genau im Pause-Modus ist das am teuersten: kein Zyklus,
        kein Repeat-Guard, kein Ledger korrigiert hier nach. Was verlorengeht,
        ist verloren — und die Karte zeigt dank Optimistic-Update trotzdem den
        Wunschwert.

        Sendet deshalb ueber `safe_service_call`, den einen Sendepfad des
        Projekts (blocking + Timeout + Logging mit Raumnamen), aber mit
        UI-Parametern statt denen des Hintergrundzyklus:

        - Timeout 10 s statt 30: Hier steht jemand vor der Karte. `gather`
          spricht die Geraete parallel an, die Wartezeit ist also das langsamste
          Geraet, nicht die Summe.
        - Keine Retries: Der Zyklus-Standard waere 30 s + 2 s + 4 s Backoff, bis
          zu ~96 s pro Geraet. Fuer einen Handgriff an der Karte ist der Retry,
          dass man nochmal drueckt.

        Der Aufrufer setzt den optimistischen UI-Wert VOR diesem Aufruf — sonst
        haengt die Karte an der Geraete-Antwort.
        """
        # Self-Referenzen filtern: sonst Endlosschleife, falls diese
        # Tempix-Entity versehentlich in `config.trvs` steht.
        target_trvs = [t for t in self._coordinator.config.trvs if t != self.entity_id]
        if not target_trvs:
            return
        # `entity_id` aus den Nutzdaten strippen: es wuerde die pro-TRV gesetzte
        # entity_id ueberschreiben und den Aufruf auf die falsche Entity lenken.
        payload = {k: v for k, v in service_data.items() if k != "entity_id"}
        _LOGGER.debug(
            "Manual Mode active - Passthrough %s %s to %s", service, payload, target_trvs
        )
        results = await asyncio.gather(
            *(
                safe_service_call(
                    self.hass, self._coordinator.config.name,
                    "climate", service, {"entity_id": trv, **payload},
                    timeout=PASSTHROUGH_TIMEOUT_S,
                    max_retries=PASSTHROUGH_RETRIES,
                )
                for trv in target_trvs
            ),
            return_exceptions=True,
        )
        # Timeout und HomeAssistantError protokolliert `safe_service_call` selbst.
        # Hier landet nur der Rest — eine Integration, die etwas anderes wirft —
        # und der ist ueberhaupt erst sichtbar, WEIL der Aufruf jetzt blockiert.
        # Pro Geraet gefangen, damit ein kaputtes TRV nicht den Ausgang der
        # anderen verdeckt.
        for trv, result in zip(target_trvs, results):
            if isinstance(result, BaseException):
                _LOGGER.warning("Passthrough %s failed for %s: %s", service, trv, result)

    def _ensure_override_enabled(self) -> None:
        """Eingabe an der Karte ablehnen, wenn der temporäre Override aus ist.

        PB-11 (c): Vorher verwarf die Karte `set_temperature`/`set_hvac_mode`
        still mit einem DEBUG-Log — der Regler sprang zurück, ohne Grund.
        Jetzt dieselbe übersetzte Meldung wie am Override-Schalter (AU-5).
        """
        if getattr(self._coordinator.config, "enable_temporary_manual_override", False):
            return
        _LOGGER.debug(
            "TPX [%s]: card input rejected – 'Enable Temporary Manual Override' is OFF",
            self._coordinator.config.name,
        )
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="override_disabled",
            translation_placeholders={"name": self._coordinator.config.name},
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Handle HVAC mode changes from the UI."""
        if getattr(self._coordinator.config, "manual_mode", False):
            # PASSTHROUGH MODE: Forward directly to ALL hardware TRVs.
            # Optimistic UI ZUERST: Der Send blockiert jetzt bis zur
            # Geraete-Antwort — wuerde die Karte erst danach aktualisiert,
            # stuende sie so lange still. Die Eingabe ist sofort sichtbar; ob sie
            # angekommen ist, beantwortet das Log, nicht der Regler.
            self._optimistic_hvac_mode = hvac_mode
            # Deadlock guard: switching OFF→HEAT/COOL while no
            # target is known would make HA hide the temperature slider
            # permanently. Inject a fallback so the slider stays usable.
            # NICHT bei dry/fan_only: dort GEHOERT kein Sollwert hin, ein
            # injizierter waere eine Zahl, die das Geraet ignoriert.
            if self._needs_target_fallback(hvac_mode):
                self._optimistic_target_temp = 20.0
            self.async_write_ha_state()
            await self._passthrough_send("set_hvac_mode", {"hvac_mode": hvac_mode})
            return

        self._ensure_override_enabled()

        self._coordinator.activate_temporary_manual_override(
            new_temp=None,
            new_hvac=str(hvac_mode),
            source="ui",
            now=datetime.now(UTC)
        )
        # Optimistic UI update for immediate feedback
        self._optimistic_hvac_mode = hvac_mode
        # Deadlock guard (see passthrough branch above)
        if self._needs_target_fallback(hvac_mode):
            self._optimistic_target_temp = 20.0
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Handle temperature changes from the UI."""
        if getattr(self._coordinator.config, "manual_mode", False):
            # PASSTHROUGH MODE: Forward directly to ALL hardware TRVs.
            # Optimistic UI ZUERST — Begruendung in `_passthrough_send`.
            if ATTR_TEMPERATURE in kwargs:
                self._optimistic_target_temp = float(kwargs[ATTR_TEMPERATURE])
            if ATTR_HVAC_MODE in kwargs:
                self._optimistic_hvac_mode = HVACMode(kwargs[ATTR_HVAC_MODE])
            self.async_write_ha_state()
            await self._passthrough_send("set_temperature", kwargs)
            return

        self._ensure_override_enabled()

        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return

        hvac_mode = kwargs.get(ATTR_HVAC_MODE)

        self._coordinator.activate_temporary_manual_override(
            new_temp=float(temp), 
            new_hvac=str(hvac_mode) if hvac_mode else None, 
            source="ui", 
            now=datetime.now(UTC)
        )
        # Optimistic UI update for immediate feedback
        self._optimistic_target_temp = float(temp)
        if hvac_mode:
            self._optimistic_hvac_mode = HVACMode(hvac_mode)
        self.async_write_ha_state()
