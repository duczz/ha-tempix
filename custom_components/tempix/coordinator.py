"""
Tempix – Coordinator.

Manages state listeners, timers, and applies changes to TRVs.
Maps every blueprint trigger to an HA event listener.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any
from datetime import timedelta, datetime, UTC

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Context, CoreState, HomeAssistant, Event, callback
from homeassistant.util import dt as dt_util
from homeassistant.helpers import entity_registry as er_helper
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_state_report_event,
    async_call_later,
    async_track_time_interval,
    async_track_point_in_time,
)

from custom_components.tempix.const import (
    CONF_LEARNED_CLIMATE_RATE,
    CONF_PARTY_MODE_SWITCH,
    CONF_GUEST_MODE_SWITCH,
    CALIBRATION_MODE_OFF,
    CALIBRATION_MODE_GENERIC,
    AGGRESSIVE_MODE_CALIBRATION,
    INVALID_STATES,
    SCHEDULING_MODE_CALENDAR,
    ClimateState,
    DEFAULT_VACATION_TEMP,
    PARALLEL_VALIDATION,
    V2_ACTIVE,
    ACTIVE_ENGINE,
    DOMAIN,
)
from custom_components.tempix.config_model import TempixConfig
from custom_components.tempix.config_validation import (
    ISSUE_TRANSLATION_KEYS as _CONFIG_ISSUE_KEYS,
    validate_config,
)
from custom_components.tempix.engine import TempixEngine
from custom_components.tempix.parallel_validation import ParallelValidator
from custom_components.tempix.clock import SystemClock
from custom_components.tempix.hardware_dispatcher import HardwareDispatcher
from custom_components.tempix.models.capability_map import DeviceCapabilityMap
from custom_components.tempix.models.hardware_command import HardwareCommand
from custom_components.tempix.models.health import SnapshotValidity
from custom_components.tempix.models.priorities import GoalClass
from custom_components.tempix.models.trigger_event import TriggerEvent
from custom_components.tempix.snapshot_builder import TEMP_TOLERANCE, parse_float
from custom_components.tempix.coordinator_scene import SceneManager
from custom_components.tempix.coordinator_appliers import (
    CalibrationApplier,
    ValvePositioner,
    async_apply_trv_change,
    safe_service_call,
)
from custom_components.tempix.coordinator_learning import ClimateRateLearner
from homeassistant.helpers.storage import Store
from .store_migration import MigratingStore
from homeassistant.helpers.debounce import Debouncer

_LOGGER = logging.getLogger(__name__)

# Gemeinsamer Kontext ALLER eigenen `calendar.get_events`-Abrufe (raumuebergreifend).
# Nach jedem Entity-Service-Aufruf aktualisiert HA pollende Entities (z. B.
# local_calendar) per `async_update_ha_state(True)` UNTER DEM KONTEXT DES AUFRUFS
# (helpers/service.py) — das feuert ein state_reported. Ohne Markierung loest der
# eigene Abruf damit ueber `_on_calendar_reported` den naechsten aus (gemessen:
# Abruf alle 5 s = Debounce-Periode). Ein pro-Raum-Kontext genuegt nicht:
# Raeume mit demselben Kalender wuerden sich gegenseitig anstossen.
CALENDAR_FETCH_CONTEXT = Context()

# Diagnostic-only attributes – state changes in these are ignored
_DIAG_ATTRS: frozenset[str] = frozenset({
    "battery_level", "linkquality", "rssi", "battery", "volt", "pressure"
})

# A joining TRV rarely reports its state in ONE event: mode first, attributes
# a moment later. Both the initial join and the return from `unavailable`
# hold this window open, so the follow-up events are not mistaken for a
# hand on the device.
_JOIN_STUTTER_WINDOW = timedelta(seconds=5)
# Lebensdauer eines Eintrags in `_pending_commands` (Echo-Fenster der
# Override-Erkennung). Eine Zahl fuer das Verwerfen UND fuer das
# Wiederherstellen nach einem Reload.
_PENDING_COMMAND_TTL_SECONDS = 180

# The room is not being controlled at all (paused/uncertain, crash loop, or a
# dead actuator) for this long -> raise a Repair issue. 15 min is the practical
# floor that still filters transients: the clock RESETS on any recovery, so
# brief flaps (DECT dropout, reboot, reload) never accumulate — only a
# continuous outage counts (lowered from 2h: an uncontrolled room must
# surface promptly, not hours late). Not
# user-configurable on purpose.
_CONTROL_LOST_THRESHOLD_SECONDS = 15 * 60
# Generic calibration is degraded but the room still runs — softer, seasonal
# (FRITZ summer mode), so it keeps the patient 2h threshold.
_CALIBRATION_ISSUE_THRESHOLD_SECONDS = 2 * 3600

# The six health-clock start times, persisted in the domain store via
# _set_health_clock so a restart doesn't silently reset every stuck room's
# Repair grace period to zero. Single source of truth —
# _async_save_domain_state, _async_restore_domain_state, and the
# persistence tests all hold against this list.
HEALTH_CLOCK_ATTRS = (
    "_uncertainty_start_time",
    "_error_loop_start",
    "_calib_missing_start",
    "_trv_missing_start",
    "_degraded_start_time",
    "_dead_reference_start_time",
)

def _looks_like_entity_id(value: Any) -> bool:
    """``domain.object_id`` — a dot alone is not enough.

    Adjustment ``comfort``/``eco`` may be a NUMBER or an entity reference
    (``get_adjustment_comfort`` tries ``float()`` first, then ``_float_state``).
    A number written as a string — ``"19.0"`` — passes a bare ``"." in value``
    check, so the domain part has to be alphabetic to tell the two apart.
    """
    if not isinstance(value, str):
        return False
    domain, _, object_id = value.partition(".")
    return bool(domain) and bool(object_id) and domain.replace("_", "").isalpha()


@dataclass
class CommandInfo:
    """Represents a command sent to a TRV to track expected echoes."""
    target_temperature: float | None
    hvac_mode: str | None
    timestamp: float

@dataclass
class TemporaryManualOverride:
    """Represents an active manual override."""
    active: bool
    source: str  # "ui" or "physical"
    timestamp: datetime
    schedule_revision: int
    target_temperature: float | None
    hvac_mode: str | None

class TempixCoordinator:
    """State management and TRV control – mirrors blueprint actions."""

    def __init__(
        self, hass: HomeAssistant, config: TempixConfig, engine: TempixEngine, entry_id: str,
    ) -> None:
        self.hass = hass
        self.config = config
        self.engine = engine
        self.entry_id = entry_id

        self._ha_started_listener: Any = None
        self._listeners: list = []
        self._last_update: dict | None = None
        # Zuletzt bekannte Kalender-Events je Kalender-ID. Ueberbrueckt einen
        # kurz unerreichbaren Kalender, statt ihn zu loeschen.
        self._last_calendar_events: dict[str, list[dict[str, Any]]] = {}
        # Hat das zuletzt ausgewertete TRV-Ereignis selbst einen Zyklus
        # eingereiht? Steuert, ob `_on_state_change` den Debounce spart.
        self._trv_event_queued_update: bool = False
        self._trigger_timer: Any = None
        self._reeval_timer: Any = None
        # Precise V2 boundary wakeup (gated on ACTIVE_ENGINE).
        self._v2_wakeup_timer: Any = None
        self._update_lock = asyncio.Lock()
        self._updates_enabled = False
        self._refresh_in_progress: bool = False
        self._refresh_pending: bool = False
        self._ready_time: datetime | None = None
        self._uncertainty_start_time: datetime | None = None
        self._paused_issue_active: bool = False
        # Post-flip the loss-of-control (paused) issue must track
        # V2, not V1's (expected) uncertainty. True when V2 drove a SAFE
        # (non-UNSAFE) decision this cycle; reset per cycle in _do_update.
        self._v2_controlled: bool = True
        # True, sobald der V2-Block im laufenden Zyklus erreicht wurde. Der
        # aeussere `except` in `async_update` braucht das: ein Absturz VOR dem
        # Block heisst post-flip, dass V2 gar nicht fuhr (Raum ungeregelt);
        # danach hat der V2-Block den Zyklus selbst verbucht.
        self._v2_block_reached: bool = False
        # Crash-loop tracking: _do_update() raising every cycle is a different
        # failure mode than "uncertain" — it never sets _uncertainty_start_time,
        # so the paused issue would stay silent forever.
        self._error_loop_start: datetime | None = None
        self._failing_issue_active: bool = False
        # Generic calibration needs the TRV's internal temperature; a TRV that
        # stops reporting it (e.g. FRITZ summer mode, DECT dropout) degrades
        # calibration silently — the entity itself stays valid, so neither
        # uncertainty nor the paused issue ever fires.
        self._calib_missing_start: datetime | None = None
        self._calib_issue_active: bool = False
        # An unavailable/unknown TRV entity is skipped in calculate_changes
        # (INVALID_STATES) without a command ever being sent — the circuit
        # breaker only trips on *failed sends*, so a dead actuator is fully
        # silent: cycle "succeeds", status shows Comfort, nothing is driven.
        self._trv_missing_start: datetime | None = None
        self._trv_issue_active: bool = False
        # Which of the two issues is up: the paused WARNING (manual_mode) or
        # the ERROR. Only meaningful while `_trv_issue_active`.
        self._trv_issue_paused: bool = False
        # DEGRADED validity (fallback applied, still usable) has no owner:
        # `_v2_controlled` only reacts to UNSAFE, so a room stuck
        # DEGRADED for any reason (external room sensor down → TRV fallback,
        # unresolved schedule) drives on a compromised input indefinitely with
        # no Repair issue and no more than a DEBUG log line — the exact "must
        # not legitimize systematic misheating forever" health.py warns about.
        # Generic by design: covers every
        # current and future DEGRADED source via `validity_reasons`, no
        # bespoke per-cause tracking needed.
        self._degraded_start_time: datetime | None = None
        self._degraded_issue_active: bool = False

        # Tote Referenzen. Eine konfigurierte Entity, die es nicht
        # mehr gibt, meldet sich von selbst nirgends — sie erzeugt keinen
        # Fehler, nur stilles Fehlverhalten. Ein geloeschter Kalender hat so
        # schon die Abwesenheitserkennung abgeschaltet; gefunden wurde das,
        # weil der Nutzer das SYMPTOM meldete. Dieses Issue schliesst genau
        # diese Luecke.
        self._dead_reference_start_time: datetime | None = None
        self._dead_reference_issue_active: bool = False
        # Zuletzt gemeldete Config-Befunde (code -> details). Nur zum
        # Entprellen bei dynamischen Updates; das Aufraeumen stuetzt sich NICHT
        # darauf (nach einem Reload ist es leer), sondern loescht beim Setup
        # jeden Config-Code, der nicht mehr zutrifft.
        self._config_findings: dict[str, str] = {}
        self._dead_reference_reported: tuple[str, ...] = ()

        # Public state for sensors
        self.current_hvac: str = "off"
        self.current_temperature: float | None = None
        self.current_reason: str = ""
        self.current_state: ClimateState = ClimateState.PAUSED
        self.last_changes: list[dict] = []
        self.last_calibrations: dict[str, float] = {}
        self.last_generic_offsets: dict[str, float] = {}
        # Dispatch-Bilanz des letzten Zyklus (nur Diagnose, siehe `_do_update`).
        self._last_dispatch: list[dict[str, Any]] = []

        # ── V2 Parallel Validation (pre-flip: dry-run, post-flip: V2 drives HW) ──
        # Macht den pro Zyklus gebauten TempixSnapshot für Sensoren/Entities
        # erreichbar (in ParallelValidator.run() ist er nur eine lokale
        # Variable). Set von ParallelValidator.run() direkt nach dem
        # Snapshot-Bau; bleibt None, solange kein V2-Zyklus lief (siehe
        # _should_run_v2 / V2_ACTIVE).
        self._v2_last_snapshot = None       # TempixSnapshot | None
        # Erste fehlgeschlagene Snapshot-Erneuerung in der Pause → WARNING,
        # danach DEBUG (siehe `_refresh_paused_snapshot`).
        self._pause_snapshot_warned = False
        # Schon gemeldete Roh-Werte ungültiger Kalender-`hvac:`-Tags; der
        # Validator (pro Zyklus neu) teilt sich dieses Set (AU-6).
        self._warned_hvac_tags: set[str] = set()
        self.v2_last_result = None          # DecisionResult | None
        self.v2_last_diff = None            # DecisionDiff | None (letzter MIT Abweichung)
        self.v2_recent_diffs: deque = deque(maxlen=5)
        self.v2_eval_count: int = 0
        self.v2_diff_count: int = 0
        self.v2_error_count: int = 0
        self._v2_logged_signature: tuple | None = None  # Diff-Log-Flut-Schutz
        self._v2_error_signature: tuple | None = None    # Error-Log-Flut-Schutz
        # Letzte GÜLTIGEN Fenster-Zustände (sid → state) — Gedächtnis für den
        # reboot-sicheren Close-Delay im V2-Fenster-Resolver (überlebt Zyklen).
        self._v2_prev_window_valid: dict[str, str] = {}
        # sid → `last_changed` des letzten ECHTEN offen→zu (Close-Delay ueber
        # mehrere Zyklen, Live-Test 2026-11-04 F5); fortgeschrieben vom
        # V2-Fenster-Resolver.
        self._v2_window_close_marks: dict = {}
        # Sensoren mit bestaetigter Oeffnungsphase (LT-F12-Rest d): nur deren
        # Schliessen setzt eine Marke.
        self._v2_window_open_phase_confirmed: set = set()
        # TRV-Event, das den nächsten V2-Zyklus triggert (für die
        # Override-Detection); im async_update konsumiert + geleert.
        self._v2_pending_trigger: TriggerEvent | None = None
        self.v2_last_detection = None       # OverrideDetectionResult | None (Schatten)
        self._v2_detection_signature: tuple | None = None  # Log-Flut-Schutz

        # Temporary Override & Echo Tracking
        self._pending_commands: dict[str, deque[CommandInfo]] = {
            trv: deque(maxlen=5) for trv in config.trvs
        }
        self._first_state_seen: dict[str, bool] = {trv: False for trv in config.trvs}
        self._startup_ignore_until: dict[str, datetime] = {}
        self._override_propagation_until: datetime | None = None
        self._override_clear_suppress_until: datetime | None = None
        # Values of the last cleared override — clear-suppress only mutes echoes
        # that still carry these values, real user input passes through.
        self._cleared_override_temp: float | None = None
        self._cleared_override_hvac: str | None = None
        self._override_blocked_warned: bool = False
        self._temporary_manual_override: TemporaryManualOverride | None = None
        self._schedule_revision: int = 0
        self._schedule_transition_pending: bool = False
        self._last_schedule_fingerprint: str | None = None  # None = never computed
        # Vergleichswert fuer den sched_next-Suppress-Guard
        # in _sched_next_diff_suppressible() -- _schedule_comfort_signal()
        # (Kalender: is_calendar_comfort_active(), Helper: is_scheduler_active()),
        # NICHT should_set_comfort() (Begruendung siehe dort).
        # Tri-state, NICHT aus dem Store restauriert (kein Recovery
        # noetig -- bei None greift ohnehin "nicht unterdruecken", das ist
        # fail-safe fuer den ersten Zyklus nach Neustart/Reload).
        self._last_comfort_decision: bool | None = None

        self._entity_callbacks: list = []
        self._option_timers: dict[str, Any] = {}
        self._option_timer_expires: dict[str, datetime] = {}
        self._background_tasks: set[asyncio.Task] = set()

        # Circuit breaker: tracks consecutive failures per TRV entity
        # { entity_id: {"failures": int, "retry_after": datetime | None} }
        self._cb_state: dict[str, dict] = {}
        self._cb_store = MigratingStore(
            Store(hass, 1, f"tempix/cb_state_{entry_id}"),
            Store(hass, 1, f"tempix.cb_state.{entry_id}"),
            config.name,
        )

        # Domain State persistence
        self._initialized: bool = False
        self._domain_store = MigratingStore(
            Store(hass, 1, f"tempix/domain_state_{entry_id}"),
            Store(hass, 1, f"tempix_domain_state_{entry_id}"),
            config.name,
        )
        # Store superset: the V2 SystemContext/override
        # rides under the additive `v2_context` key in the SAME domain store.
        # Held here as an OPAQUE blob — V1 never deserializes it, so V1 can
        # never choke on V2 data and always writes it back verbatim. That
        # preservation is what makes the 1-line rollback safe: a rolled-back
        # V1 keeps the v2_context intact for a future re-flip.
        self._v2_context_raw: dict | None = None
        self._domain_save_debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=2.0,
            immediate=False,
            function=self._async_save_domain_state,
        )

        # Long-lived shadow ledger owner. Holds the
        # CommandLedger across cycles AND reboots (own Store file) so confirm()
        # and the override detection have the real last-commanded state. Pre-flip
        # it runs with an empty capability map and only its LEDGER side is used
        # (record_sent / confirm / ledger_view / persistence). Post-flip the
        # ParallelValidator feeds the real capabilities via configure(), and the
        # dispatcher plans and sends (see dry_run below).
        self._ledger_store = MigratingStore(
            Store(hass, 1, f"tempix/ledger_{entry_id}"),
            Store(hass, 1, f"tempix_ledger_{entry_id}"),
            config.name,
        )
        self._v2_dispatcher = HardwareDispatcher(
            hass, SystemClock(), DeviceCapabilityMap(devices=()),
            # dry_run is coupled to the flag — the long-lived
            # dispatcher only sends for real once V2 drives. Pre-flip ("v1")
            # this is True (unchanged, shadow-ledger only); at the flip it
            # becomes False so dispatch_commands() actually reaches the hardware.
            dry_run=(ACTIVE_ENGINE != "v2"),
            name=self.config.name, action_delay=self.config.action_delay,
            on_cb_change=self._schedule_dispatcher_cb_save,
            on_send=self._track_v2_send,
            # Sendeschleifen enden an Geraete-Grenzen, sobald Unload das
            # Tor geschlossen hat (siehe `async_unload`).
            should_abort=lambda: not self._updates_enabled,
        )
        self._ledger_save_debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=2.0,
            immediate=False,
            function=self._async_save_ledger,
        )
        # The dispatcher's own circuit-breaker state
        # survives reboots in its own Store (symmetric to the ledger). Inert
        # pre-flip — dry_run never trips the breaker, so this stays empty until
        # V2 drives; on_cb_change above schedules a debounced save when it does.
        self._dispatcher_cb_store = MigratingStore(
            Store(hass, 1, f"tempix/dispatcher_cb_{entry_id}"),
            Store(hass, 1, f"tempix_dispatcher_cb_{entry_id}"),
            config.name,
        )
        self._dispatcher_cb_save_debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=2.0,
            immediate=False,
            function=self._async_save_dispatcher_cb,
        )
        # Kalender-Reported-Events (siehe `_on_calendar_reported`): Coordinator-
        # basierte Kalender (caldav, remote_calendar, google) melden bei JEDEM
        # Poll ein state_reported. Der Timer startet beim ersten Report, weitere
        # Reports innerhalb der 5 s fallen in denselben Abruf (HA-`Debouncer` mit
        # immediate=False, kein Verlaengern) — so kostet ein Poll-Schwall nicht
        # pro Event einen `calendar.get_events`.
        self._calendar_report_debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=5.0,
            immediate=False,
            function=self._async_refetch_calendar_if_changed,
        )

        # Hysteresis persistence
        self._hysteresis_dirty: bool = False
        self._store = MigratingStore(
            Store(hass, 1, f"tempix/hysteresis_{entry_id}"),
            Store(hass, 1, f"tempix_hysteresis_{entry_id}"),
            config.name,
        )
        self._save_debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=2.0,
            immediate=False,
            function=self._async_save_hysteresis_state,
        )
        self.engine.set_on_dirty(self._on_hysteresis_dirty)

        # ── helper objects ────────────────────────────────────────────────
        self._scene_manager = SceneManager(
            hass,
            config.trvs,
            config.action_delay.total_seconds(),
            config.name,
            entry_id,
        )
        self._calib_applier = CalibrationApplier(hass, config, engine, config.name)
        self._valve_positioner = ValvePositioner(hass, config, engine, config.name)
        self._rate_learner = ClimateRateLearner(hass, config, engine, entry_id)
        self._prev_party: bool = False

        # Cutover-Beobachtbarkeit: WELCHE Engine faehrt, steht sonst nirgends im
        # Log. Ohne diese Zeile kann die Rollback-Probe nur zeigen,
        # dass nichts abstuerzt — nicht, dass V1 wieder steuert. Bewusst INFO und
        # NICHT an `debug_mode` gehaengt (debug_log): die Probe soll ohne
        # Sonderkonfiguration nachweisbar sein. Eine Zeile pro Raum beim Setup,
        # kein Zyklus-Spam.
        _LOGGER.info(
            "Tempix [%s]: aktive Engine = %s (%s)",
            self.config.name, ACTIVE_ENGINE,
            "V2 steuert, V1 rechnet als Beobachter mit" if ACTIVE_ENGINE == "v2"
            else "V1 steuert, V2 rechnet als Beobachter mit",
        )

    def debug_log(self, msg: str) -> None:
        """Log debug message with coordinator prefix."""
        if self.config.debug_mode:
            _LOGGER.info("TPX Coord [%s]: %s", self.config.name, msg)
        else:
            _LOGGER.debug("TPX Coord [%s]: %s", self.config.name, msg)

    def apply_config(self, config: TempixConfig) -> None:
        """Swap the typed config on the coordinator AND all helper objects.

        Dynamic (no-reload) option updates create a NEW TempixConfig instance.
        CalibrationApplier, ValvePositioner and ClimateRateLearner hold their
        own reference — without this swap they keep reading the stale config
        until the next full reload (and diverge from switch-toggle mutations).
        """
        self.config = config
        # Die Engine gehoert in DIESE Liste, nicht an die Aufrufstelle.
        # Der Docstring versprach seit jeher „coordinator AND all
        # helper objects" — dass es trotzdem funktionierte, hing daran, dass
        # `async_reload_entry` `engine.config` eine Zeile vorher separat setzte.
        # Ein zweiter Aufrufer haette die Engine still auf der alten Config
        # weiterlaufen lassen. Muss VOR `bind_config` stehen: der Lerner
        # stempelt `learned_climate_rate` auf genau diese Instanz zurueck.
        self.engine.config = config
        self._calib_applier._config = config
        self._valve_positioner._config = config
        # Kein blosser Swap: der Lerner haelt den gelernten Wert selbst und
        # stempelt ihn auf die neue Config zurueck. Ohne das fiel
        # `learned_climate_rate` bei JEDER Options-Aenderung auf 1.0 — der Wert
        # steht nicht in `entry.options`, nur im Lern-Store.
        self._rate_learner.bind_config(config)
        self._scene_manager._action_delay_secs = config.action_delay.total_seconds()
        # Der Name kommt aus entry.title und aendert sich beim Umbenennen des
        # Eintrags. Die Helfer bekamen ihn als Konstruktor-Argument, halten also
        # eine eigene Kopie — ohne diesen Swap loggen sie nach einer
        # Umbenennung weiter unter dem Anlagenamen (nur Log-Label, keine ID).
        self._scene_manager._name = config.name
        self._calib_applier._name = config.name
        self._valve_positioner._name = config.name
        # Post-Flip die LAUTESTE Log-Quelle der Integration: CB-Skips,
        # Kalibrierschreiben, Ventilstellungen und jeder fehlgeschlagene
        # `safe_service_call` tragen dieses Label. Fehlte es hier, liefen
        # ausgerechnet die Zeilen, die man nach einer Umbenennung sucht,
        # weiter unter dem Anlagenamen. `action_delay` kommt mit, weil der
        # Dispatcher ihn wie die Helfer als Konstruktor-Kopie haelt.
        self._v2_dispatcher._name = config.name
        self._v2_dispatcher._action_delay = config.action_delay
        # Nach dem Swap: dynamische Updates (Schalter/Number/Options ohne
        # Reload) umgehen `async_setup`. Entprellt, siehe `_sync_config_issues`.
        self._sync_config_issues()

    def _sync_config_issues(self, *, initial: bool = False) -> None:
        """Semantische Config-Befunde als Repair-Issues (nicht-fatal).

        `initial=True` (Setup) raeumt jeden Config-Code, der nicht mehr zutrifft,
        unabhaengig vom Gedaechtnis ab — nach Reload ist `_config_findings` leer,
        ein im alten Lauf gemeldeter Code bliebe sonst stehen. Danach nur bei
        Aenderung, weil jede Bedienung (Schalter, Number) durch `apply_config`
        laeuft. Wirft nie.
        """
        try:
            findings = {f.code: f.details for f in validate_config(self.config)}
            if not initial and findings == self._config_findings:
                return
            for code, key in _CONFIG_ISSUE_KEYS.items():
                issue_id = f"config_{code}_{self.entry_id}"
                if code not in findings:
                    ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                    continue
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=key,
                    translation_placeholders={
                        "name": self.config.name,
                        "details": findings[code],
                    },
                )
                if initial or self._config_findings.get(code) != findings[code]:
                    _LOGGER.warning(
                        "%s: config warning %s (%s) — Repair issue raised",
                        self.config.name, code, findings[code],
                    )
            self._config_findings = findings
        except Exception:  # noqa: BLE001 - Validierung darf Setup/Update nie brechen
            _LOGGER.debug("config issue sync failed", exc_info=True)

    def _create_tracked_task(self, coro) -> asyncio.Task:
        """Create a fire-and-forget task and track it to prevent leaks on unload."""
        task = self.hass.async_create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def async_set_temporary_option(
        self, key: str, value: bool, duration_mins: int | None = None
    ) -> None:
        """Set an option temporarily or permanently and handle timers."""
        # Input validation
        if not self._validate_option(key, value, duration_mins):
            _LOGGER.error("%s: Invalid option value for %s: %s (duration=%s)", self.config.name, key, value, duration_mins)
            return

        if key in self._option_timers:
            self._option_timers[key]()
            del self._option_timers[key]
        self._option_timer_expires.pop(key, None)

        entry_id = self.entry_id
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if not entry:
            return

        new_options = dict(entry.options)
        new_options[key] = value
        self.hass.config_entries.async_update_entry(entry, options=new_options)

        # If we enable it with a duration, start a timer to disable it
        if value and duration_mins and duration_mins > 0:
            self.debug_log(f"Setting temporary option {key}={value} for {duration_mins} mins")

            if key in self._option_timers and self._option_timers[key]:
                self._option_timers[key]()

            @callback
            def _timer_finished(_now):
                self.debug_log(f"Temporary option {key} expired. Resetting to False.")
                self._option_timers.pop(key, None)
                self._option_timer_expires.pop(key, None)
                self._create_tracked_task(
                    self.async_set_temporary_option(key, False)
                )

            # Keep track of when it expires for persistence
            self._option_timer_expires[key] = datetime.now(UTC) + timedelta(minutes=duration_mins)
            self._option_timers[key] = async_call_later(
                self.hass, duration_mins * 60, _timer_finished
            )
        else:
            self.debug_log(f"Setting option {key}={value} (no timer)")

        # Persist timer changes right away (domain state must survive a
        # restart). Without this a
        # party/guest timer set after the last override event only reached
        # the store on graceful unload — and was lost on crash/power loss.
        self._create_tracked_task(self._domain_save_debouncer.async_call())

    @callback
    def cancel_option_timer(self, key: str) -> None:
        """Drop a running temporary timer for ``key`` — RAM *und* Store.

        Die Schalter-Entities schreiben ``entry.options`` direkt und gingen an
        der Timer-Buchhaltung vorbei. Wer Party per Service auf
        2 h stellt und danach den Schalter benutzt, hatte weiter einen scharfen
        Timer auf einem Wert, den der Schalter besitzt: er kassierte spaeter ein
        dauerhaftes „An", das der Nutzer ausdruecklich gesetzt hatte.

        Der Save-Anstoss gehoert zwingend dazu. ``_option_timer_expires.pop()``
        aendert nur den Speicher; geschrieben wird erst beim naechsten
        Debouncer-Lauf, und **kein Regelzyklus speichert pauschal** (nur ein
        Health-Uhr-Wechsel, ein unterdrueckter Fingerprint oder ein geloeschter
        Override stossen dort einen Save an). Ohne den Anstoss ueberlebt der
        abgelaufene Eintrag den Neustart, und ``_clear_expired_temporary_options`` raeumt
        beim Restore genau das „An" ab, das dieser Cancel schuetzen sollte.
        """
        cancel = self._option_timers.pop(key, None)
        had_expiry = self._option_timer_expires.pop(key, None) is not None
        if cancel is None and not had_expiry:
            return
        if cancel:
            cancel()
        self.debug_log(f"Cancelled temporary timer for {key} (switch takes ownership)")
        self._create_tracked_task(self._domain_save_debouncer.async_call())

    def async_add_listener(self, callback_func):
        """Register an entity callback for state updates."""
        self._entity_callbacks.append(callback_func)

        def remove_listener():
            if callback_func in self._entity_callbacks:
                self._entity_callbacks.remove(callback_func)

        return remove_listener

    def _call_listeners(self):
        """Notify all registered listeners (entities) of a change."""
        for callback_func in self._entity_callbacks:
            try:
                callback_func()
            except Exception as exc:
                _LOGGER.error("Error in entity callback: %s", exc, exc_info=True)

    # ── circuit breaker persistence ──────────────────────────────────────────

    async def _cb_load(self) -> None:
        try:
            stored = await self._cb_store.async_load()
            if stored:
                for eid, state in stored.items():
                    retry_raw = state.get("retry_after")
                    self._cb_state[eid] = {
                        "failures": state.get("failures", 0),
                        "retry_after": datetime.fromisoformat(retry_raw) if retry_raw else None,
                    }
        except Exception as exc:
            _LOGGER.warning("%s: Failed to load circuit breaker state: %s", self.config.name, exc)

    async def _cb_save(self) -> None:
        try:
            serialized = {
                eid: {
                    "failures": state["failures"],
                    "retry_after": state["retry_after"].isoformat() if state["retry_after"] else None,
                }
                for eid, state in self._cb_state.items()
            }
            await self._cb_store.async_save(serialized)
        except Exception as exc:
            _LOGGER.warning("%s: Failed to save circuit breaker state: %s", self.config.name, exc)

    # ── domain state persistence ──────────────────────────────────────────────

    async def _async_restore_domain_state(self) -> None:
        self._initialized = False
        try:
            restored = await self._domain_store.async_load()
            if not isinstance(restored, dict):
                return

            override_data = restored.get("override")
            if isinstance(override_data, dict) and "timestamp" in override_data and "expires_at" in override_data:
                ts = datetime.fromisoformat(override_data["timestamp"])
                expires_at = datetime.fromisoformat(override_data["expires_at"])
                now = datetime.now(UTC)
                
                if now < expires_at:
                    self._temporary_manual_override = TemporaryManualOverride(
                        active=override_data.get("active", True),
                        target_temperature=override_data.get("target_temperature"),
                        hvac_mode=override_data.get("hvac_mode"),
                        source=override_data.get("source", "physical"),
                        timestamp=ts,
                        schedule_revision=override_data.get("schedule_revision", self._schedule_revision)
                    )
                    self.debug_log(f"Restored manual override: {override_data.get('target_temperature')}°C, expires at {expires_at}")
                else:
                    self.debug_log("Ignored restored override because it has expired.")

            # Restore option timers (e.g. Party Mode Timer)
            option_timers_data = restored.get("option_timers")
            if isinstance(option_timers_data, dict):
                now = datetime.now(UTC)
                expired_keys: list[str] = []
                for key, expires_str in option_timers_data.items():
                    try:
                        expires_at = datetime.fromisoformat(expires_str)
                        if expires_at <= now:
                            # Waehrend der Downtime abgelaufen.
                            # Der Wert lebt in `entry.options`, der Timer im
                            # Domain-Store — ohne diesen Zweig blieb Party/Gaeste
                            # nach einem Neustart EWIG an, bis jemand es von Hand
                            # ausschaltet. `check_party_mode` hat keine eigene
                            # Ablaufpruefung, es liest nur das Flag.
                            expired_keys.append(key)
                            continue

                        remaining_seconds = (expires_at - now).total_seconds()
                        self._option_timer_expires[key] = expires_at

                        @callback
                        def _restored_timer_finished(_now, k=key):
                            self.debug_log(f"Restored temporary option {k} expired. Resetting to False.")
                            self._option_timers.pop(k, None)
                            self._option_timer_expires.pop(k, None)
                            self._create_tracked_task(self.async_set_temporary_option(k, False))

                        self._option_timers[key] = async_call_later(self.hass, remaining_seconds, _restored_timer_finished)
                        self.debug_log(f"Restored timer for {key}, expires in {remaining_seconds}s")
                    except Exception as e:
                        self.debug_log(f"Failed to restore timer for {key}: {e}")

                if expired_keys:
                    # Eigener Guard: ein Fehler hier darf nicht den Rest des
                    # Restores (Schedule-Fingerprint, V2-Kontext) verschlucken.
                    try:
                        self._clear_expired_temporary_options(expired_keys)
                    except Exception as exc:
                        _LOGGER.warning(
                            "%s: Failed to clear expired temporary options %s: %s",
                            self.config.name, expired_keys, exc,
                        )

            # Restore the six health-clock start times so an already
            # 14-minutes-degraded room is one minute from its Repair after a
            # restart, not fifteen again. A corrupted individual entry is
            # skipped (clock stays None, same as never having started) —
            # never lets one bad value abort the rest of the restore.
            health_clocks = restored.get("health_clocks")
            if isinstance(health_clocks, dict):
                for attr, iso_value in health_clocks.items():
                    if attr not in HEALTH_CLOCK_ATTRS:
                        continue
                    try:
                        setattr(self, attr, datetime.fromisoformat(iso_value))
                    except (TypeError, ValueError) as exc:
                        self.debug_log(f"Failed to restore health clock {attr}: {exc}")

            restored_fp = restored.get("last_schedule_fingerprint")
            if isinstance(restored_fp, str):  # None-Guard + type-check against corrupted store
                self._last_schedule_fingerprint = restored_fp

            # Carry the V2 context blob forward opaquely.
            # Never deserialized here — only preserved so a V1 save (incl. after
            # a rollback) does not drop it.
            v2_ctx = restored.get("v2_context")
            self._v2_context_raw = v2_ctx if isinstance(v2_ctx, dict) else None
        except Exception as exc:
            _LOGGER.warning("%s: Failed to restore Tempix domain state: %s", self.config.name, exc)
        finally:
            self._initialized = True

    def _clear_expired_temporary_options(self, keys: list[str]) -> None:
        """Reset options whose temporary timer expired during the downtime.

        Gegenstueck zum Override-Restore darueber: der laeuft rein im Speicher,
        ein abgelaufener Override ist mit dem Nicht-Wiederherstellen erledigt.
        Party/Gaeste haben ZWEI Heimaten — Flag in ``entry.options``, Ablauf im
        Domain-Store — und nur die zweite kennt den Ablauf.

        Drei Orte muessen mit, sonst faellt der alte Wert wieder ein:
        ``entry.options`` (ueberlebt den Neustart), das lebende
        ``TempixConfig``-Objekt (Coordinator und Engine teilen sich EINE
        Instanz) und dessen ``_raw`` (die Vergleichsbasis von
        ``async_reload_entry``).

        Bewusst NICHT ueber ``async_set_temporary_option``: das laeuft asynchron
        und schreibt pro Key einmal in die Optionen. Hier gilt ein einziger
        ``async_update_entry`` fuer alle Keys — zum Aufrufzeitpunkt (innerhalb
        von ``async_setup``) ist der Update-Listener noch nicht registriert, ein
        Reload kann also nicht dazwischenfunken.
        """
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            return

        # `is True` statt truthy: nur echte Bool-Flags werden zurueckgesetzt.
        # Ein Key mit Timer, dessen Wert kein Schalter ist (z.B. ein gelernter
        # Zahlenwert), hat kein definiertes "aus" und bleibt unangetastet.
        stale = [k for k in keys if getattr(self.config, k, None) is True]
        if not stale:
            return

        new_options = dict(entry.options)
        for key in stale:
            new_options[key] = False
            setattr(self.config, key, False)
            if isinstance(getattr(self.config, "_raw", None), dict):
                self.config._raw[key] = False
            _LOGGER.info(
                "%s: Temporary option %s expired during downtime — reset to off.",
                self.config.name, key,
            )
        self.hass.config_entries.async_update_entry(entry, options=new_options)
        # Der abgelaufene Eintrag steht noch im Store: die Schleife oben hat ihn
        # per `continue` uebersprungen, also kam er nie in
        # `_option_timer_expires`, aus dem der Save sich speist. Ohne diesen
        # Anstoss bliebe er dort stehen, bis zufaellig etwas anderes speichert —
        # und Domain-Saves laufen nur anlassbezogen (Setter, Health-Uhr-Wechsel,
        # unterdrueckter Fingerprint), nicht pauschal pro Zyklus. Ohne einen
        # solchen Anlass also unter Umstaenden tagelang. Schaltet der
        # Nutzer in der Zeit dauerhaft ein und HA stuerzt ab, raeumt derselbe
        # stale Eintrag beim naechsten Start erneut ab.
        self._create_tracked_task(self._domain_save_debouncer.async_call())

    async def _async_save_domain_state(self) -> None:
        state = {
            "schema_version": 1,
            "override": None,
            "option_timers": {k: v.isoformat() for k, v in self._option_timer_expires.items()},
            "last_schedule_fingerprint": getattr(self, "_last_schedule_fingerprint", None),
            # Only the clocks actually running are written — a room
            # with every clock at None (the common, healthy case) adds
            # nothing to the store.
            "health_clocks": {
                attr: getattr(self, attr).isoformat()
                for attr in HEALTH_CLOCK_ATTRS
                if getattr(self, attr) is not None
            },
        }
        
        if self._temporary_manual_override and self._temporary_manual_override.active:
            override = self._temporary_manual_override
            state["override"] = {
                "active": override.active,
                "source": override.source,
                "timestamp": override.timestamp.isoformat(),
                "expires_at": (override.timestamp + timedelta(hours=24)).isoformat(), # V1 defaults to 24 hours
                "schedule_revision": override.schedule_revision,
                "target_temperature": override.target_temperature,
                "hvac_mode": override.hvac_mode
            }

        # Preserve the opaque V2 context blob alongside
        # the V1 keys. Additive — absent on old 1.7.0 stores, only written
        # once V2 owns an override. This is the single writer to the shared
        # store, so including it here prevents the full-rebuild from clobbering.
        if self._v2_context_raw is not None:
            state["v2_context"] = self._v2_context_raw

        try:
            await self._domain_store.async_save(state)
        except Exception as exc:
            _LOGGER.warning("%s: Failed to save domain state: %s", self.config.name, exc)

    def set_v2_context(self, raw: dict | None) -> None:
        """Update the opaque V2 context blob and schedule a debounced save.

        The write path is wired now but only populated
        once V2 owns the override (after the flip V1 still owns it).
        Callers pass an already-serialized ``ActiveOverride.to_dict()`` (or
        ``None`` to clear); the coordinator stores it opaquely and never
        deserializes it. Routed through the same debouncer as the V1 keys, so
        there is still exactly one writer to the shared store.
        """
        self._v2_context_raw = raw if isinstance(raw, dict) else None
        self._create_tracked_task(self._domain_save_debouncer.async_call())

    # ── V2 shadow ledger persistence ─────────────────────────────────────────

    async def _async_restore_ledger(self) -> None:
        """Rehydrate the shadow ledger after a reboot. Entries come back STALE
        (restore_ledger forces it) so the first TRV echo post-reboot is matched
        as our own command, never re-confirmed, never resent."""
        try:
            data = await self._ledger_store.async_load()
            now = datetime.now(UTC)
            self._v2_dispatcher.restore_ledger(data, now)
            self._seed_pending_commands_from_ledger(now)
        except Exception as exc:
            _LOGGER.warning("%s: Failed to restore V2 ledger: %s", self.config.name, exc)

    def _seed_pending_commands_from_ledger(self, now: datetime) -> None:
        """Echo-Erwartung ueber einen Reload retten.

        `_evaluate_trv_event` prueft eingehende TRV-Events gegen
        `_pending_commands`, nicht gegen das Ledger. Nach einem Reload war die
        Queue leer, obwohl das Ledger das Kommando noch kannte — das spaete
        Echo eines Kommandos, das beim Neuladen noch in der Luft war, wurde zum
        `physical`-Override (gemessen an einem Klimageraet:
        `cool @ 16.0`, Reload 2 s nach dem Senden, danach Override 16.0/cool und
        die AC kuehlte real auf 16 Grad).

        Uebernommen wird nur, was noch eine Echo-Erwartung ist: unbestaetigt und
        juenger als die Queue-TTL (`_prune_expired_commands`, 180 s). Ein
        bestaetigter Eintrag wartet auf nichts mehr und wuerde sonst einen
        passenden Handgriff schlucken. Das Seeding selbst aendert das Ledger
        nicht und sendet nichts. Ein spaeteres volles Ack bestaetigt den
        Eintrag danach wie ueblich (`confirm()` prueft nicht auf STALE — dieselbe
        Luecke zum Versprechen „nie re-confirmed" in `restore_ledger`).
        Das Ledger kennt je Geraet nur das LETZTE Kommando;
        aeltere, beim Reload noch fliegende Kommandos desselben Geraets
        bleiben ohne Echo-Erwartung.
        """
        for entry in self._v2_dispatcher.ledger_view().entries:
            queue = self._pending_commands.get(entry.entity_id)
            if queue is None or entry.confirmed_at is not None:
                continue
            age = (now - entry.sent_at).total_seconds()
            if age < 0 or age > _PENDING_COMMAND_TTL_SECONDS:
                continue
            queue.append(CommandInfo(
                target_temperature=entry.target_temperature,
                hvac_mode=entry.hvac_mode,
                timestamp=entry.sent_at.timestamp(),
            ))

    async def _async_save_ledger(self) -> None:
        try:
            await self._ledger_store.async_save(self._v2_dispatcher.ledger_to_storage())
        except Exception as exc:
            _LOGGER.warning("%s: Failed to save V2 ledger: %s", self.config.name, exc)

    async def _async_restore_dispatcher_cb(self) -> None:
        """Rehydrate the dispatcher's circuit-breaker state after a reboot so a
        TRV still inside its backoff window is not hammered again on the first
        post-restart cycle (V1 parity — the coordinator does the same via
        _cb_load)."""
        try:
            data = await self._dispatcher_cb_store.async_load()
            self._v2_dispatcher.restore_cb(data)
        except Exception as exc:
            _LOGGER.warning("%s: Failed to restore V2 dispatcher CB: %s", self.config.name, exc)

    async def _async_save_dispatcher_cb(self) -> None:
        try:
            await self._dispatcher_cb_store.async_save(self._v2_dispatcher.cb_to_storage())
        except Exception as exc:
            _LOGGER.warning("%s: Failed to save V2 dispatcher CB: %s", self.config.name, exc)

    def _schedule_dispatcher_cb_save(self) -> None:
        """on_cb_change hook (passed to the dispatcher): debounced persist of the
        circuit-breaker state whenever a device trips or recovers."""
        self._create_tracked_task(self._dispatcher_cb_save_debouncer.async_call())

    def _record_v2_command(self, entity_id: str, temperature: float | None,
                           hvac_mode: str | None) -> None:
        """Shadow-record a command V1 just sent to the hardware into the V2
        ledger and schedule a debounced save. Gives confirm() and the override
        detection the real
        last-commanded state without any own hardware call."""
        self._v2_dispatcher.record_sent(entity_id, hvac_mode, temperature, datetime.now(UTC))
        self._create_tracked_task(self._ledger_save_debouncer.async_call())

    def _log_v2_detection(self, detection, trigger, name: str) -> None:
        """Record + log the SHADOW override detection.

        Kept strictly OUT of the parity DecisionDiff: V2's detection may
        legitimately differ from V1's echo handling, and that must not pollute
        the '0 diffs' parity signal. DEBUG only (an expected event, not an
        anomaly); flood-protected by signature."""
        self.v2_last_detection = detection
        if detection is None:
            return
        sig = (detection.source, bool(detection.matched),
               bool(detection.calibration_explained))
        if sig != self._v2_detection_signature and _LOGGER.isEnabledFor(logging.DEBUG):
            self._v2_detection_signature = sig
            _LOGGER.debug(
                "Tempix V2 shadow override detection (%s): matched=%s source=%s "
                "calibration_explained=%s trigger=%s",
                name, detection.matched, detection.source,
                detection.calibration_explained,
                trigger.entity_id if trigger else None,
            )

    # ── hysteresis persistence ───────────────────────────────────────────────

    async def _async_restore_hysteresis(self) -> None:
        try:
            data = await self._store.async_load()
            if data and "outside_ok" in data:
                self.engine.restore_outside_state(data["outside_ok"])
        except Exception as exc:
            _LOGGER.warning("%s: Failed to load hysteresis state: %s", self.config.name, exc)

    def _on_hysteresis_dirty(self) -> None:
        """Called by engine when outside_ok changes."""
        # Das Flag wird IMMER
        # gesetzt, nur der Save ausgelassen, solange HA noch startet. Vorher
        # verwarf der frühe Return beides — eine Änderung von `outside_ok`
        # während des Starts war damit dauerhaft weg, und der gespeicherte Wert
        # blieb stale, bis nach dem Start zufällig die nächste Änderung kam.
        # Das Flag allein kostet nichts: `_async_save_hysteresis_state` und der
        # Unload-Flush schreiben beim nächsten Anlass mit.
        self._hysteresis_dirty = True
        if not self.hass.is_running:
            return
        self._create_tracked_task(self._save_debouncer.async_call())

    async def _async_save_hysteresis_state(self) -> None:
        if not self._hysteresis_dirty:
            return
        self._hysteresis_dirty = False
        try:
            await self._store.async_save({"outside_ok": self.engine.outside_ok})
        except Exception as exc:
            _LOGGER.warning("%s: Failed to save hysteresis state: %s", self.config.name, exc)

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Register listeners matching every blueprint trigger."""
        self.debug_log(f"Starting coordinator setup (ID: {self.entry_id[:6]})")
        self._sync_config_issues(initial=True)
        await self._scene_manager.async_load()
        await self._rate_learner.async_load()
        await self._cb_load()
        await self._async_restore_hysteresis()
        await self._async_restore_domain_state()
        if V2_ACTIVE:
            await self._async_restore_ledger()
            await self._async_restore_dispatcher_cb()
        # Discard any window scene saved before this boot. TRVs may have been
        # manually changed during the downtime; restoring stale states would undo that.
        # If the window is still open after restart, Tempix will set eco immediately anyway.
        self._scene_manager.clear("window")

        if self.hass.state == CoreState.running:
            self.debug_log("HA already running, starting coordinator immediately")
            await self._start_coordinator()
        else:
            self.debug_log("Waiting for Home Assistant to start...")
            # Capture bus listener unsubscriber
            self._ha_started_listener = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._on_ha_started
            )

    async def _on_ha_started(self, _event: Event) -> None:
        self._ha_started_listener = None
        await self._start_coordinator()

    async def _start_coordinator(self) -> None:
        """Start the coordinator without delays."""
        if self._updates_enabled:
            self.debug_log("Coordinator already started, skipping redundant start.")
            return

        self._register_listeners()
        self._updates_enabled = True

        # Pre-populate _first_state_seen for TRVs that HA already knows about.
        # Without this, a TRV that was connected hours before Tempix starts processing
        # would still get its first state change suppressed as if it were a startup sync.
        for trv in self.config.trvs:
            state = self.hass.states.get(trv)
            if state and state.state not in ("unavailable", "unknown"):
                self._first_state_seen[trv] = True
                self.debug_log(f"Pre-populated _first_state_seen: {trv} (state={state.state})")

        self._ready_time = datetime.now(UTC)
        self.debug_log(f"Coordinator ready at {self._ready_time}")

        # ── Periodic Data Fetchers (decoupled from update loop) ──
        interval = timedelta(minutes=self.config.calendar_scan_interval)
        needs_initial_update = True

        if self.config.scheduling_mode == SCHEDULING_MODE_CALENDAR or self.config.holiday_calendar:
            self._listeners.append(async_track_time_interval(
                self.hass, self._async_fetch_and_update_calendar, interval
            ))
            # Attempt an immediate fetch. Cloud calendars (e.g. Google) that haven't
            # finished their server sync will fail silently (debug-level) and retry
            # on the next state-change or periodic timer.
            # Note: each wrapper calls async_update after its own fetch. Helper-mode rooms
            # with a holiday calendar and schedulers will therefore run two updates on
            # startup — this is
            # intentional: if the calendar fetch fails (integration not yet ready), the
            # schedule update still runs independently with its own data.
            await self._async_fetch_and_update_calendar()
            needs_initial_update = False

        if self.config.scheduling_mode != SCHEDULING_MODE_CALENDAR and self.config.schedulers:
            self._listeners.append(async_track_time_interval(
                self.hass, self._async_fetch_and_update_schedule, interval
            ))
            await self._async_fetch_and_update_schedule()
            needs_initial_update = False

        if needs_initial_update:
            try:
                await self.async_update()
            except asyncio.CancelledError:
                _LOGGER.warning(
                    "%s: Initial update cancelled (likely integration reload before HA was ready). "
                    "Will retry on next state change.",
                    self.config.name,
                )

    async def _async_fetch_calendar_events(self) -> None:
        """Fetch agenda for all configured calendars for deep scanning."""
        calendars = list(self.config.calendar)
        if self.config.holiday_calendar and self.config.holiday_calendar not in calendars:
            calendars.append(self.config.holiday_calendar)
        if not calendars:
            return

        now = datetime.now(UTC)
        start = now - timedelta(days=1)
        end = now + timedelta(days=7)

        all_events: dict[str, list[dict[str, Any]]] = {}

        # Zuletzt bekannte Events je Kalender — ein uebersprungener Kalender
        # BEHAELT sie. Vorher loeschte der Ausfall die Daten, die
        # ihn haetten ueberbruecken sollen: `set_calendar_events(all_events)`
        # ersetzt das ganze Dict, ein per `continue` uebersprungener Kalender
        # fehlte darin, und `is_holiday_today` fiel auf seinen Event-Fallback
        # zurueck, der damit ebenfalls leer war. Beide Wege fielen gemeinsam
        # aus — am Feiertag lief dann der Werktagsplan, bis zum naechsten
        # erfolgreichen Fetch (Default-Intervall 15 min).
        #
        # Nebenbei wird damit die Meldung im Aufrufer wahr, die schon vorher
        # "using last cached data" versprach: Sie galt nur, wenn die GANZE
        # Funktion warf — beim Pro-Kalender-Skip warf nichts.
        #
        # Dasselbe Muster wie beim Fenster-Fallback und beim
        # Proximity-Kanal: bei Unsicherheit den letzten bekannten Stand
        # halten, statt "keine Events" zu behaupten.
        previous = self._last_calendar_events

        for cal_id in calendars:
            if not self.hass.states.get(cal_id):
                _LOGGER.debug("Calendar entity %s not available, keeping last known events", cal_id)
                if cal_id in previous:
                    all_events[cal_id] = previous[cal_id]
                continue
            try:
                self.debug_log(f"Fetching calendar events for {cal_id}")
                params = {
                    "entity_id": cal_id,
                    "start_date_time": start.isoformat(),
                    "end_date_time": end.isoformat(),
                }
                response = await safe_service_call(
                    self.hass, self.config.name,
                    "calendar", "get_events",
                    params,
                    return_response=True,
                    ha_error_log_level=logging.DEBUG,
                    context=CALENDAR_FETCH_CONTEXT,
                )

                if response and cal_id in response:
                    events = response[cal_id].get("events", [])
                    all_events[cal_id] = events
                    self.debug_log(f"Fetched {len(events)} events for {cal_id}")
                elif response and "events" in response:
                    events = response.get("events", [])
                    all_events[cal_id] = events
                elif cal_id in previous:
                    # Antwort unbrauchbar → letzten Stand halten statt loeschen.
                    all_events[cal_id] = previous[cal_id]
            except Exception as exc:
                _LOGGER.error("Error fetching calendar events for %s: %s", cal_id, exc, exc_info=True)
                if cal_id in previous:
                    all_events[cal_id] = previous[cal_id]

        # Nur konfigurierte Kalender bleiben erhalten — entfernte fallen von
        # selbst heraus, weil die Schleife ueber `calendars` laeuft.
        self._last_calendar_events = all_events
        self.engine.set_calendar_events(all_events)

    async def _async_fetch_schedule_slots(self) -> None:
        """Fetch timeslots for all configured schedulers via schedule.get_schedule."""
        schedulers = self.config.schedulers
        if not schedulers:
            return

        all_slots: dict[str, list[dict[str, Any]]] = {}

        for sched_id in schedulers:
            if not self.hass.states.get(sched_id):
                _LOGGER.debug("Scheduler entity %s not available, skipping fetch", sched_id)
                continue
            try:
                self.debug_log(f"Fetching schedule slots for {sched_id}")
                response = await safe_service_call(
                    self.hass, self.config.name,
                    "schedule", "get_schedule",
                    {"entity_id": sched_id},
                    return_response=True,
                )
                if response:
                    # Response: {entity_id: {weekday: [{'from': time, 'to': time}]}}
                    data = response.get(sched_id, {})
                    all_slots[sched_id] = data
                    self.debug_log(f"Fetched schedule for {sched_id}: {list(data.keys())}")
            except Exception as exc:
                _LOGGER.warning("Error fetching schedule slots for %s: %s", sched_id, exc)

        self.engine.set_schedule_slots(all_slots)

    async def _async_fetch_and_update_calendar(self, _now: datetime | None = None) -> None:
        """Fetch calendar events and trigger a recalculation."""
        if not self._updates_enabled:
            return
        try:
            await self._async_fetch_calendar_events()
        except Exception as exc:
            _LOGGER.warning("%s: Calendar fetch failed, using last cached data: %s", self.config.name, exc)
        await self.async_update()

    @staticmethod
    def _calendar_event_fingerprint(
        events: dict[str, list[dict[str, Any]]],
    ) -> frozenset[tuple[str, str, str]]:
        """Normalisierte (cal_id, start, end)-Menge — Vergleichsbasis statt des
        rohen Dicts.

        Grund (gemessen): `is_calendar_comfort_active()` /
        `_get_active_calendar_event()` (engine_calendar.py) schreiben
        `ev["calendar_id"] = cal_id` IN JEDES Event-Dict hinein, als
        Seiteneffekt der Engine-Auswertung des VORHERIGEN Zyklus — dieselben
        Objekte, die hier als `before` verglichen werden. Ein frisch
        gefetchtes Dict hat den Schluessel noch nicht, ein roher `!=`-Vergleich
        sieht deshalb bei jedem Kalender-Modus-Raum (nicht Helper-Modus, dort
        laeuft die Mutation nie) staendig "geaendert". Live beobachtet: 5 von
        6 Raeumen loesten so JEDEN Poll einen Zyklus aus, nur der einzige
        Helper-Modus-Raum korrekt keinen.
        """
        return frozenset(
            (cal_id, str(ev.get("start")), str(ev.get("end")))
            for cal_id, cal_events in events.items()
            for ev in (cal_events or [])
        )

    async def _async_refetch_calendar_if_changed(self) -> None:
        """Kalender neu abrufen, aber nur bei geaenderten Terminen neu rechnen.

        Ziel des entprellten `state_reported`-Pfads (`_on_calendar_reported`). Ein
        pollender Kalender (`local_calendar`, HA-`SCAN_INTERVAL` 60 s) meldet jede
        Minute ein state_reported OHNE Aenderung (gemessen): der Abruf
        muss sein (nur er erkennt ein Loeschen ohne Zustandswechsel), ein Zyklus
        daneben zusaetzlich zum Heartbeat aber nicht. Der Abruf selbst aktualisiert
        Engine-Cache und `_last_calendar_events` in jedem Fall.
        """
        if not self._updates_enabled:
            return
        before = self._calendar_event_fingerprint(self._last_calendar_events)
        try:
            await self._async_fetch_calendar_events()
        except Exception as exc:
            _LOGGER.warning("%s: Calendar fetch failed, using last cached data: %s", self.config.name, exc)
            return
        if self._calendar_event_fingerprint(self._last_calendar_events) != before:
            self.debug_log("Kalender-Poll: Aenderung erkannt, Zyklus wird ausgeloest")
            await self.async_update()
        else:
            self.debug_log("Kalender-Poll: unveraendert, kein Zyklus")

    async def _async_fetch_and_update_schedule(self, _now: datetime | None = None) -> None:
        """Fetch schedule slots and trigger a recalculation."""
        if not self._updates_enabled:
            return
        try:
            await self._async_fetch_schedule_slots()
        except Exception as exc:
            _LOGGER.warning("%s: Schedule slots fetch failed, using last cached data: %s", self.config.name, exc)
        await self.async_update()

    def _register_listeners(self) -> None:
        """Track entities for state changes = blueprint triggers."""
        tracked: list[str] = []

        def add_if_entity(eid: Any) -> None:
            # `_looks_like_entity_id` statt `"." in eid`: die
            # Adjustment-Werte unten duerfen auch Zahlen sein, und ein "19.0"
            # wurde bisher als Entity getrackt. Der Listener feuerte fuer so
            # eine ID nie — falsch war es trotzdem.
            if _looks_like_entity_id(eid):
                tracked.append(eid)
            elif isinstance(eid, list):
                for item in eid:
                    add_if_entity(item)

        add_if_entity(self.config.trvs)

        add_if_entity(self.config.temp_sensor)

        add_if_entity(self.config.schedulers)
        add_if_entity(self.config.scheduler_selector)

        add_if_entity(self.config.persons)

        for val in (
            self.config.guest_mode,
            self.config.presence_sensor,
            self.config.scheduler_presence,
            self.config.season_mode_entity,
            self.config.outside_temp_sensor,
            self.config.weather_entity,
        ):
            add_if_entity(val)

        add_if_entity(self.config.calendar)
        add_if_entity(self.config.holiday_calendar)

        # Proximity: config holds a device_id — track all its entities
        if self.config.proximity_entity:
            ent_reg = er_helper.async_get(self.hass)
            for entry in er_helper.async_entries_for_device(ent_reg, self.config.proximity_entity):
                tracked.append(entry.entity_id)

        add_if_entity(self.config.window_sensors)

        # Adjustments: comfort/eco may reference entity_ids (e.g. input_number.foo)
        for entry in self.config.adjustments or []:
            if isinstance(entry, dict):
                add_if_entity(entry.get("comfort"))
                add_if_entity(entry.get("eco"))

        # Deduplicate and filter non-string values (e.g. booleans)
        tracked = list(set(eid for eid in tracked if isinstance(eid, str)))

        if tracked:
            unsub = async_track_state_change_event(
                self.hass, tracked, self._on_state_change
            )
            self._listeners.append(unsub)

        # Schedule-Edit ohne Zustands-/Attributwechsel (z. B. anderer Wochentag =
        # Umleitungsziel) erzeugt nur EVENT_STATE_REPORTED, kein state_changed.
        if self.config.scheduling_mode != SCHEDULING_MODE_CALENDAR and self.config.schedulers:
            sched_ids = [s for s in self.config.schedulers if isinstance(s, str)]
            if sched_ids:
                self._listeners.append(async_track_state_report_event(
                    self.hass, sched_ids, self._on_scheduler_reported
                ))

        # Kalender: Ein Termin-Loeschen/-Aendern, das weder Zustand noch Attribute
        # (= nur der aktuelle/naechste Termin) beruehrt — beendeter oder spaeterer
        # Termin —, erzeugt nur EVENT_STATE_REPORTED (gemessen mit
        # local_calendar). Gleiche Bedingung wie der periodische Abruf (s. Start).
        if self.config.scheduling_mode == SCHEDULING_MODE_CALENDAR or self.config.holiday_calendar:
            cal_ids = list(dict.fromkeys(
                c for c in (*self.config.calendar, self.config.holiday_calendar)
                if isinstance(c, str) and c
            ))
            if cal_ids:
                self._listeners.append(async_track_state_report_event(
                    self.hass, cal_ids, self._on_calendar_reported
                ))

        self.debug_log(f"tracking {len(tracked)} entities")

        # Periodic heartbeat (every 1 min)
        self._listeners.append(async_track_time_interval(
            self.hass, self._on_heartbeat, timedelta(minutes=1)
        ))

    @callback
    def _on_scheduler_reported(self, event: Event) -> None:
        """Scheduler wurde geschrieben, Zustand unveraendert: Slots neu laden."""
        if not self._updates_enabled:
            return
        self._create_tracked_task(self._async_fetch_and_update_schedule())

    @callback
    def _on_calendar_reported(self, event: Event) -> None:
        """Kalender wurde geschrieben, Zustand und Attribute unveraendert: neu abrufen.

        Ergaenzt den periodischen Scan, ersetzt ihn nicht — Aenderungen, die nur
        auf dem Server liegen, sieht HA selbst erst beim Poll der Integration.
        """
        if not self._updates_enabled:
            return
        # Echo des eigenen Abrufs (siehe CALENDAR_FETCH_CONTEXT) — sonst Endlosschleife.
        ctx = getattr(event, "context", None)
        if ctx is not None and ctx.id == CALENDAR_FETCH_CONTEXT.id:
            return
        self._create_tracked_task(self._calendar_report_debouncer.async_call())

    @callback
    def _on_state_change(self, event: Event) -> None:
        """Handle any tracked entity change."""
        if not self._updates_enabled:
            return

        entity_id = event.data.get("entity_id", "")

        # Scheduler-Aenderung (Grenzwechsel oder Edit): Slot-Cache aktualisieren;
        # die Neuberechnung laeuft weiter ueber den Debounce unten.
        if (
            self.config.scheduling_mode != SCHEDULING_MODE_CALENDAR
            and entity_id in self.config.schedulers
        ):
            self._create_tracked_task(self._async_fetch_schedule_slots())
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        # Calendar state change → trigger immediate re-fetch + recalculation.
        # Skip if the calendar is still unavailable/syncing to avoid fetch spam on startup.
        if entity_id in self.config.calendar or entity_id == self.config.holiday_calendar:
            if new_state and new_state.state not in ("unavailable", "unknown"):
                self._create_tracked_task(self._async_fetch_and_update_calendar())
            return

        # TRV changes (HVAC or Temperature) - Evaluate for manual override
        if entity_id in self.config.trvs and new_state:
            if new_state.state in ("unavailable", "unknown"):
                return
            old_temp = old_state.attributes.get("temperature") if old_state else None
            new_temp = new_state.attributes.get("temperature")
            old_hvac = old_state.state if old_state else None
            new_hvac = new_state.state

            if old_temp != new_temp or old_hvac != new_hvac:
                # V2 shadow: value-based ack of our last
                # command (two-phase ledger commit) + capture the TRV delta as the
                # TriggerEvent for the next cycle's override detection. Runs
                # alongside V1's own echo handling; never influences V1.
                # Gate: V2_ACTIVE, not PARALLEL_VALIDATION — post-flip the
                # ledger MUST be confirmed from real TRV events, otherwise every
                # entry stays PENDING and `_dispatch_one` skips all further sends.
                if V2_ACTIVE:
                    self._v2_dispatcher.confirm(
                        entity_id, new_hvac, new_temp, datetime.now(UTC))
                    self._create_tracked_task(self._ledger_save_debouncer.async_call())
                    ctx = getattr(event, "context", None)
                    self._v2_pending_trigger = TriggerEvent(
                        entity_id=entity_id,
                        old_hvac=old_hvac,
                        old_temp=parse_float(old_temp),
                        new_hvac=new_hvac,
                        new_temp=parse_float(new_temp),
                        timestamp=event.time_fired,
                        is_user_context=bool(ctx and getattr(ctx, "user_id", None)),
                    )
                # Nur HIER ist entscheidbar, ob die Entity vorher ueberhaupt
                # da war: `old_hvac=None` ist auch der Default der Methode und
                # traegt die Unterscheidung deshalb nicht.
                returned = old_state is None or old_state.state in (
                    "unavailable", "unknown")
                suppressed = self._evaluate_trv_event(
                    entity_id, new_temp, new_hvac, event.context,
                    old_temp=old_temp, old_hvac=old_hvac, resync=returned,
                )
                if suppressed or self._trv_event_queued_update:
                    # Unterdrueckt (Startup, Echo, Propagation, Clear Suppress)
                    # ODER die Aktivierung hat bereits einen Zyklus eingereiht —
                    # in beiden Faellen waere der Debounce ein zweiter Durchlauf.
                    # Ebenfalls unterdrueckt: Manual Mode (Hands-off). Dort ist
                    # der Debounce nicht doppelt, sondern unerwuenscht.
                    #
                    # NICHT abkuerzen, wenn die Aktivierung nichts eingereiht hat
                    # (Option `enable_temporary_manual_override` aus): dann ist
                    # dieser Debounce der einzige Weg, den Sollwert nach der
                    # manuellen Drehung wieder durchzusetzen.
                    return

        # Ignore identical state
        if old_state and new_state and old_state.state == new_state.state:
            # Collect all changed attributes; only pure diagnostic attributes
            # (_DIAG_ATTRS) are ignored below.
            changed_attrs = set(new_state.attributes.keys()) - set(old_state.attributes.keys())
            for attr in new_state.attributes:
                if old_state.attributes.get(attr) != new_state.attributes.get(attr):
                    changed_attrs.add(attr)

            # If ONLY diagnostic attributes changed, ignore.
            if changed_attrs and changed_attrs.issubset(_DIAG_ATTRS):
                return

            # If no important attribute changed and state is same, ignore.
            if not changed_attrs:
                return

        delta = self.config.action_delay

        # Cancel previous trigger if still pending (Debouncing)
        if self._trigger_timer:
            self._trigger_timer()

        self._trigger_timer = async_call_later(
            self.hass, delta.total_seconds(), self._delayed_update
        )

    @callback
    def _on_heartbeat(self, _now) -> None:
        """Periodic heartbeat to ensure state is in sync."""
        self.debug_log("Heartbeat fired")
        self._create_tracked_task(self.async_update())

    @callback
    def _delayed_update(self, _now) -> None:
        self._create_tracked_task(self.async_update())

    def _schedule_v2_wakeup(self, next_at) -> None:
        """Schedule a precise re-evaluation at the next policy boundary
        ADDITIVE to the 1-min heartbeat safety net —
        it only makes the cycle MORE timely, never less. Replaces any prior
        V2 wakeup so no timer leaks (cf. _reeval_timer). Ignores None / past
        boundaries. Gated behind ACTIVE_ENGINE=='v2' at the call site, so it
        is inert (V1 timing unchanged) until the flip."""
        if self._v2_wakeup_timer:
            self._v2_wakeup_timer()
            self._v2_wakeup_timer = None
        if next_at is None or next_at <= datetime.now(UTC):
            return
        self._v2_wakeup_timer = async_track_point_in_time(
            self.hass, self._delayed_update, next_at
        )

    @callback
    def _evaluate_trv_event(
        self, entity_id: str, new_temp: float | None, new_hvac: str | None, context: Any,
        old_temp: float | None = None, old_hvac: str | None = None,
        resync: bool = False,
    ) -> bool:
        """Handle TRV state or attribute changes, checking for manual overrides.

        old_temp/old_hvac carry the previous TRV state and enable partial-echo
        detection (integrations that ack a combined command as two events).

        ``resync`` marks an event whose PREVIOUS state was missing or
        ``unavailable``/``unknown`` — the entity is coming back, nobody touched
        it. Only the caller can decide this; ``old_hvac=None`` is also
        this method's default and therefore not a usable signal.

        Returns:
            True  – the event was suppressed (Manual Mode, Startup, Echo,
                    Propagation, Clear Suppress).
                    The caller should NOT start the debounce timer.
            False – kein Suppress-Grund fuer den Override-Pfad; der Aufrufer
                    entscheidet ueber seinen Debounce. Normalfall: es wurde
                    versucht, einen temporaeren Override zu aktivieren, und OB
                    dabei ein Zyklus eingereiht wurde, steht in
                    ``_trv_event_queued_update`` — bei ausgeschalteter Option
                    passiert naemlich nichts, und dann MUSS der Aufrufer seinen
                    Debounce starten.
                    Zweiter Fall: ``resync`` — kein Override, aber
                    der Raum braucht den Zyklus erst recht.
        """
        # Frisch pro Ereignis — sonst traegt der Aufrufer den Wert des
        # vorherigen Events und spart den Debounce faelschlich ein.
        self._trv_event_queued_update = False

        # 1. Manual Mode Guard (Hands-off Modus)
        # Event layer: prevent creation of new shadow states
        if getattr(self.config, "manual_mode", False):
            self.debug_log(f"Manual override is active. Ignoring TRV event from {entity_id}.")
            return True

        # 2. Startup Suppression (Per-Entity Initial-Sync)
        now = datetime.now(UTC)
        if not self._first_state_seen.get(entity_id, False):
            self._first_state_seen[entity_id] = True
            # Double-join stutter protection (5s)
            self._startup_ignore_until[entity_id] = now + _JOIN_STUTTER_WINDOW
            self.debug_log(f"Startup suppression: Ignoring FIRST state from {entity_id}")
            return True

        ignore_until = self._startup_ignore_until.get(entity_id)
        if ignore_until and now < ignore_until:
            # Das Fenster war urspruenglich wertblind — JEDES Event
            # darin verschwand, auch ein echter Handgriff, der zufaellig in
            # dieselben 5s fiel. Jetzt nur noch unterdruecken, wenn das Event
            # entweder die laufende Rueckkehr fortsetzt (Mode unveraendert)
            # oder ohnehin unser eigenes aktuelles Ziel bestaetigt — beides
            # kein Handgriff. Alles andere faellt durch zur regulaeren
            # Override-Erkennung unten.
            still_settling = self._is_still_settling(old_hvac, old_temp, new_hvac, new_temp)
            matches_own_target = self._matches_command(
                new_temp, new_hvac, self.current_temperature, self.current_hvac)
            if still_settling or matches_own_target:
                self.debug_log(f"Startup suppression: Ignoring double-join state from {entity_id}")
                return True
            _LOGGER.info(
                "TPX [%s]: %s meldete waehrend des Stotter-Fensters einen Wert, der "
                "weder die Rueckkehr fortsetzt noch unser eigenes Ziel ist (hvac=%s, "
                "temp=%s) — nicht unterdrueckt.",
                self.config.name, entity_id, new_hvac, new_temp,
            )

        # 3. Re-Sync Suppression — die Rueckkehr aus `unavailable` ist
        # kein Griff ans Geraet. `_on_state_change` blockt nur die Richtung
        # rein->`unavailable`; der Weg zurueck sah bisher aus wie eine
        # Nutzereingabe. Die Startup-Suppression darueber deckt das NICHT ab:
        # sie greift einmal pro Entity und Coordinator-Leben, nicht erneut,
        # wenn eine FREMDE Integration ihre Entities spaeter neu registriert.
        # Gemessen: ein Read-Timeout gegen die FRITZ!Box
        # setzte `fritzbox.climate` neu auf, worauf mehrere DECT-Raeume einen
        # Override `temp=None, hvac=off, source=physical` aktivierten. Zwei
        # davon blieben stundenlang stehen — in der Heizsaison waere das
        # unterdrueckte Heizung, nicht bloss ein Schoenheitsfehler.
        if resync:
            # Die zurueckkehrende Plattform meldet ihre Werte oft ueber mehrere
            # Events (erst der Mode, dann die Attribute). Das ist dasselbe
            # Stottern wie beim Doppel-Join — also dasselbe Fenster, sonst
            # schluepft das zweite Event durch und legt den Override doch an.
            self._startup_ignore_until[entity_id] = now + _JOIN_STUTTER_WINDOW
            if V2_ACTIVE:
                # Der Ledger-Eintrag von VOR dem Ausfall ist noch frisch genug,
                # um den Repeat-Guard zu triggern — der wuerde den gleich vom
                # Aufrufer eingereihten Re-Assert fuer die volle TTL schlucken
                # (gemessener Vorfall: FRITZ!Box-Read-Timeout, Raeume blieben
                # stundenlang unkorrigiert stehen).
                self._v2_dispatcher.bypass_repeat_guard_once(entity_id)
            _LOGGER.info(
                "TPX [%s]: %s ist aus '%s' zurueckgekehrt (jetzt hvac=%s, "
                "temp=%s) — als Re-Sync behandelt, keine manuelle "
                "Uebersteuerung angelegt.",
                self.config.name, entity_id, old_hvac or "unbekannt",
                new_hvac, new_temp,
            )
            # Bewusst kein `return True`: der Raum braucht nach der Rueckkehr
            # seinen Sollwert wieder. `False` ohne Aktivierung laesst den
            # Aufrufer seinen Debounce starten — sonst kaeme das Re-Assert erst
            # mit dem naechsten Heartbeat (bis zu 1 min spaeter).
            return False

        # Prune expired commands from queue (TTL = 180s)
        self._prune_expired_commands(entity_id, now)

        # 4. Clear Suppression – ignore TRV echoes after manual override clear.
        # Value-based: nur Events unterdrücken, die noch die Werte
        # des GERADE GELÖSCHTEN Overrides tragen (verspätete TRV-Callbacks).
        # Echte neue User-Eingaben mit anderen Werten passieren sofort.
        if self._override_clear_suppress_until and now < self._override_clear_suppress_until:
            if self._matches_cleared_override(new_temp, new_hvac):
                self.debug_log(
                    f"Clear suppression: Ignoring TRV echo from {entity_id} "
                    f"(still carries cleared override values temp={new_temp}, hvac={new_hvac})."
                )
                return True
            self.debug_log(
                f"Non-matching event from {entity_id} during clear-suppress window "
                f"(new_temp={new_temp}, new_hvac={new_hvac}). "
                f"Not a cleared-override echo — checking the remaining echo rules."
            )

        # 5. Queue Matching (Echo Detection)
        if self._is_queue_match(entity_id, new_temp, new_hvac):
            self.debug_log(f"Echo detected from {entity_id}: temp={new_temp}, hvac={new_hvac}. Ignoring.")
            return True

        # 6. Partial Propagation Echo (Multi-Event-Acks)
        # Manche Integrationen (z.B. Zigbee2MQTT) bestätigen ein kombiniertes
        # Mode+Temp-Kommando als ZWEI getrennte State-Events: erst kippt der Mode
        # (Temp-Attribut hält noch den alten Wert), dann folgt die Temperatur.
        # Keines der Events matcht das Kommando vollständig — aber jedes matcht es
        # in genau der Dimension, die sich geändert hat, während die ANDERE
        # Dimension noch ihrem vorherigen Wert entspricht (Ausnahme: Mode-Ack
        # aus `off`, dort gibt es keinen vorherigen Sollwert). Das ist ein Echo,
        # kein User-Eingriff; das vollständige Ack folgt mit dem nächsten Event
        # und räumt die Queue über den regulären Match auf.
        if self._is_partial_queue_match(entity_id, new_temp, new_hvac, old_temp, old_hvac):
            self.debug_log(
                f"Partial propagation echo from {entity_id}: temp={new_temp}, hvac={new_hvac} "
                f"(previous: temp={old_temp}, hvac={old_hvac}). Ignoring, awaiting full ack."
            )
            return True

        # 7. Smart Propagation Ignore
        if self._override_propagation_until and now < self._override_propagation_until:
            # Check if this event matches the currently broadcasting override.
            # If so, it's an expected propagation echo from a delayed TRV.
            if self._temporary_manual_override and self._temporary_manual_override.active:
                if self._matches_command(
                    new_temp, new_hvac,
                    self._temporary_manual_override.target_temperature,
                    self._temporary_manual_override.hvac_mode
                ):
                    self.debug_log(f"Propagation echo from {entity_id} matches broadcasting override. Ignoring.")
                    return True
                else:
                    # Kein return True hier.
                    # Ein Nicht-Match im Propagation-Fenster ist eine echte neue User-Eingabe
                    # (z.B. User ändert sofort Meinung: 25°C → 22°C innerhalb von 15s).
                    # Echo-Detection (_is_queue_match) hat Firmware-Bounces bereits vorher abgefangen.
                    # → Fall-through zu Step 8: activate_temporary_manual_override
                    self.debug_log(
                        f"Non-matching event from {entity_id} during propagation window "
                        f"(new_temp={new_temp}, new_hvac={new_hvac}). "
                        f"Treating as intentional user change, not a delayed bounce."
                    )

        # 8. Activate Temporary Override
        # `activate_temporary_manual_override` reiht den Zyklus normalerweise
        # selbst ein — dann braucht `_on_state_change` keinen Debounce mehr.
        # NICHT aber, wenn die Option aus ist: dann steigt sie ohne Update aus,
        # und der Debounce ist der einzige Weg, den Sollwert wieder
        # durchzusetzen. Das Ergebnis wird deshalb weitergereicht, statt es aus
        # dem Rueckgabewert dieser Methode zu erraten.
        source = "ui" if context and (context.user_id or context.parent_id) else "physical"
        self._trv_event_queued_update = self.activate_temporary_manual_override(
            new_temp, new_hvac, source, now)
        return False

    def _prune_expired_commands(self, entity_id: str, now: datetime) -> None:
        """Remove commands older than 180s from the pending queue."""
        queue = self._pending_commands.get(entity_id)
        if not queue:
            return
        # now.timestamp() is seconds since epoch
        current_ts = now.timestamp()
        while queue and (current_ts - queue[0].timestamp) > _PENDING_COMMAND_TTL_SECONDS:
            expired = queue.popleft()
            self.debug_log(f"Pruned expired command for {entity_id}: temp={expired.target_temperature}, hvac={expired.hvac_mode}")

    def _temps_match(self, temp_a: float | None, temp_b: float | None) -> bool:
        """Temperature match within 0.5°C tolerance (floating point / TRV rounding).

        Bewusste Design-Grenze der Override-Erkennung: 0.5 °C ist
        bei den meisten TRVs EXAKT ein Schritt. Eine echte Einzelschritt-Drehung
        des Nutzers innerhalb des Echo-Fensters (180 s nach einem eigenen
        Kommando) wird deshalb als Echo geschluckt — der Eingriff geht verloren.
        Das ist der Preis dafür, dass nicht jedes Geräte-Echo als manueller
        Eingriff zählt; ohne Toleranz wäre die Übersteuerung unbrauchbar.
        Durch einen Test gepinnt, der exakt die 0.5°-Grenze prüft.
        Dieselbe Toleranz sitzt post-Flip in V2s wertbasiertem
        ``confirm()`` — siehe ``TEMP_TOLERANCE`` in ``snapshot_builder.py``.
        """
        if temp_a is None and temp_b is None:
            return True
        if temp_a is None or temp_b is None:
            return False
        return abs(float(temp_a) - float(temp_b)) <= TEMP_TOLERANCE

    def _temps_match_exactly(self, temp_a: float | None, temp_b: float | None) -> bool:
        """Exact equality (float-noise epsilon only), for
        comparing against the device's OWN previous value — not against a
        command we sent. ``_temps_match``'s ±0.5°C is the right tolerance for
        "does this already match what we sent" (device rounding), but reused
        against ``old_temp`` in ``_is_partial_queue_match`` it swallowed every
        single-step rotation a human can make on a 0.5°-grid TRV: a real
        multi-event ack echoes the previous value EXACTLY, a person changing
        it does not. A unit test holds the reproduction.
        """
        if temp_a is None and temp_b is None:
            return True
        if temp_a is None or temp_b is None:
            return False
        return abs(float(temp_a) - float(temp_b)) <= 1e-9

    def _matches_command(
        self, temp_a: float | None, hvac_a: str | None,
        temp_b: float | None, hvac_b: str | None
    ) -> bool:
        """Check if temperature and hvac mode match."""
        if hvac_a != hvac_b:
            return False

        if hvac_a == "off" and hvac_b == "off":
            return True

        return self._temps_match(temp_a, temp_b)

    def _is_still_settling(
        self, old_hvac: str | None, old_temp: float | None,
        new_hvac: str | None, new_temp: float | None,
    ) -> bool:
        """Is this event still the SAME re-sync/double-join settling,
        not a new value? Mode must be unchanged; the temperature either
        matches too or simply wasn't reported yet (``old_temp is None`` — a
        platform re-registering fills in its attributes over several events,
        a human turning a dial does not)."""
        if old_hvac != new_hvac:
            return False
        if old_hvac == "off":
            return True
        return old_temp is None or self._temps_match(old_temp, new_temp)

    def _is_partial_queue_match(
        self, entity_id: str, new_temp: float | None, new_hvac: str | None,
        old_temp: float | None, old_hvac: str | None,
    ) -> bool:
        """Detect a partial ack of a pending command (multi-event propagation).

        Matches when exactly ONE dimension (hvac or temp) already equals a
        pending command while the OTHER dimension still holds its previous
        value. One exception: a mode ack out of `off` matches without an old
        setpoint, because a device coming from `off` has none to hold.
        The queue is intentionally NOT advanced — the full ack that
        follows pops the entry via the regular _is_queue_match.
        Conservative otherwise: without the old value of the dimension that
        has not acked yet, no partial match is claimed.
        """
        queue = self._pending_commands.get(entity_id)
        if not queue:
            return False

        for cmd in queue:
            hvac_matches = new_hvac == cmd.hvac_mode
            temp_matches = self._temps_match(new_temp, cmd.target_temperature)

            # Mode-Ack zuerst: hvac matcht das Kommando, Temp ist noch der alte Wert.
            # Exakter Vergleich gegen old_temp, nicht die 0.5°-Toleranz —
            # die ist fuer "matcht das GESENDETE Kommando" gedacht (device
            # rounding), nicht fuer "traegt noch den alten Wert". Ein echter
            # Multi-Event-Ack echot old_temp exakt; jede Drehung eines
            # Menschen tut das nicht.
            if hvac_matches and not temp_matches and old_temp is not None \
                    and self._temps_match_exactly(new_temp, old_temp):
                return True

            # Mode-Ack AUS `off` HERAUS. Ein Geraet, das aus `off`
            # kommt, hat keinen alten Sollwert, den es halten koennte —
            # `old_temp is None` ist hier kein Grund zur Vorsicht, sondern der
            # Beleg. Der Zweig darueber kann den Fall deshalb nie treffen.
            #
            # Gemessen (FRITZ Comet DECT, zwei Raeume,
            # Nachbarraum als unberuehrte Kontrolle): das Geraet quittiert ein
            # `off -> heat/X` in zwei Schritten — erst mit SEINEM eigenen
            # Komfortwert, ~10 s spaeter mit unserem. Nur EIN gesendetes
            # Kommando je Zyklus; der zweite Schritt kommt von allein und
            # raeumt die Queue ueber den regulaeren Match.
            #
            # Kausalanker ist das pending eigene Kommando, nicht der Uebergang:
            # dreht ein Mensch das Geraet von Hand aus `off` heraus, ist die
            # Queue leer und der Override entsteht wie bisher. Ein UNBEKANNTER
            # Vorzustand (`old_hvac is None`) bleibt ebenfalls aussen vor —
            # das haelt ein eigener Test fest.
            if hvac_matches and not temp_matches and old_temp is None \
                    and old_hvac == "off":
                return True

            # Temp-Ack zuerst: Temp matcht das Kommando, hvac ist noch der alte Wert
            if temp_matches and not hvac_matches and old_hvac is not None \
                    and new_hvac == old_hvac:
                return True

        return False

    def _matches_cleared_override(self, new_temp: float | None, new_hvac: str | None) -> bool:
        """Check if an event still carries the values of the just-cleared override.

        Unset override dimensions (None) act as wildcard — the override never
        commanded them, so the TRV echo carries arbitrary previous values there.
        """
        ct = self._cleared_override_temp
        ch = self._cleared_override_hvac
        if ct is None and ch is None:
            return False
        temp_ok = ct is None or self._temps_match(new_temp, ct)
        hvac_ok = ch is None or new_hvac == ch
        return temp_ok and hvac_ok

    def _is_queue_match(self, entity_id: str, new_temp: float | None, new_hvac: str | None) -> bool:
        """Check if the event matches any pending command. If so, advance queue."""
        queue = self._pending_commands.get(entity_id)
        if not queue:
            return False

        match_index = -1
        for i, cmd in enumerate(queue):
            if self._matches_command(new_temp, new_hvac, cmd.target_temperature, cmd.hvac_mode):
                match_index = i
                break

        if match_index >= 0:
            # Advance queue: remove the matched item and all older items before it
            for _ in range(match_index + 1):
                queue.popleft()
            return True
            
        return False

    def track_sent_command(self, entity_id: str, target_temperature: float | None, hvac_mode: str | None) -> None:
        """Add a command to the pending queue when Tempix sends it."""
        queue = self._pending_commands.get(entity_id)
        if queue is not None:
            now_ts = datetime.now(UTC).timestamp()
            queue.append(CommandInfo(
                target_temperature=target_temperature,
                hvac_mode=hvac_mode,
                timestamp=now_ts
            ))
            self.debug_log(f"Tracked sent command for {entity_id}: temp={target_temperature}, hvac={hvac_mode}")

    def _track_v2_send(self, command: HardwareCommand) -> None:
        """`on_send`-Hook des V2-Dispatchers — feuert je Geraet unmittelbar vor
        dem Service-Call, mit dem Kommando, das wirklich rausgeht.

        `_evaluate_trv_event` laeuft ENGINE-UNABHAENGIG und prueft eingehende
        TRV-Events gegen `_pending_commands`; gefuellt wurde die Queue aber nur
        von `_apply_v1_hardware`, das post-Flip abgeschaltet ist. Ohne diesen
        Hook schlaegt jedes `_is_queue_match` fehl und Tempix haelt das Echo
        seines EIGENEN Kommandos fuer einen manuellen Eingriff — ein falscher
        Override bei jedem Sendevorgang (gefunden durch einen Test, nicht im
        Feld).

        Das ist die Umkehrung dessen, was pre-Flip schon passierte: dort meldete
        V1 seine Sends per `_record_v2_command` in V2s Ledger. Jetzt meldet V2
        seine Sends in V1s Queue. Beides kann spaeter entfernt werden, wenn
        V1s Event-Layer verschwindet.
        """
        self.track_sent_command(
            command.entity_id, command.target_temperature, command.hvac_mode)

    def activate_temporary_manual_override(
        self, new_temp: float | None, new_hvac: str | None, source: str, now: datetime
    ) -> bool:
        """Activate a temporary manual override.

        Returns ``True``, wenn ein Zyklus eingereiht wurde — nur dann darf der
        Aufrufer seinen Debounce sparen. Vorher gab die
        Methode nichts zurueck, und `_on_state_change` schloss aus "nicht
        unterdrueckt" auf "Update laeuft schon". Das stimmt nicht, wenn die
        Option `enable_temporary_manual_override` aus ist (Migrations-Default
        fuer Bestandsnutzer): dann steigt die Methode unten ohne `async_update`
        aus, und ohne Debounce liefe GAR KEIN Zyklus — die manuelle Drehung
        bliebe bis zum naechsten Heartbeat stehen, statt zurueckgeregelt zu
        werden. Ein Early-Return im Aufrufer waere genau deshalb falsch.
        """
        if not getattr(self.config, "enable_temporary_manual_override", False):
            # Warn once per setup/reload — with the feature off (migration
            # default for existing users) EVERY physical dial turn lands here,
            # a WARNING each time would spam the log.
            if not self._override_blocked_warned:
                self._override_blocked_warned = True
                _LOGGER.warning(
                    "TPX [%s]: Manual TRV change ignored – 'Enable Temporary Manual Override' "
                    "is OFF in the options (source=%s). Further occurrences are logged at debug level.",
                    self.config.name, source,
                )
            else:
                self.debug_log(
                    f"activate_temporary_manual_override blocked – feature is OFF (source={source})"
                )
            return False

        # Cancel any active clear-suppress window – explicit re-activation takes priority
        self._override_clear_suppress_until = None
        self._cleared_override_temp = None
        self._cleared_override_hvac = None
            
        # Merge with existing active override to prevent UI clobbering
        merged_temp = new_temp
        merged_hvac = new_hvac
        if self._temporary_manual_override and self._temporary_manual_override.active:
            if merged_temp is None:
                merged_temp = self._temporary_manual_override.target_temperature
            if merged_hvac is None:
                merged_hvac = self._temporary_manual_override.hvac_mode

        self.debug_log(f"Activating temporary override: temp={merged_temp}, hvac={merged_hvac}, source={source}")
        self._temporary_manual_override = TemporaryManualOverride(
            active=True,
            source=source,
            timestamp=now,
            schedule_revision=self._schedule_revision,
            target_temperature=merged_temp,
            hvac_mode=merged_hvac
        )
        
        # Start propagation phase for 15s
        self._override_propagation_until = now + timedelta(seconds=15)
        
        # We must trigger an update so the engine incorporates the override
        self._create_tracked_task(self.async_update())
        self._create_tracked_task(self._domain_save_debouncer.async_call())
        return True

    def clear_temporary_manual_override(self, trigger_update: bool = True) -> None:
        """Deactivate the temporary manual override.

        ``trigger_update=False`` fuer Aufrufer, die INNERHALB von `_do_update`
        stehen: dort rechnet der laufende Zyklus
        unmittelbar danach ohnehin neu, der eingereihte zweite waere reine
        Wiederholung. Post-Flip ist das kein Performance-Thema mehr, sondern
        Zustand: ein zweiter Durchlauf rueckt die Kalibrier-Deadband
        (`_last_generic_offsets`) und den per-TRV-Throttle
        (`_last_calibration_time`) ein zweites Mal vor und beeinflusst damit
        die naechste Entscheidung.

        Der **Save** laeuft in JEDEM Fall — er ist kein Zyklus, sondern die
        Persistenz des geloeschten Overrides. Ihn mitzunehmen war die Falle an
        der naheliegenden "Early-Return"-Fassung dieses Fixes.
        """
        if self._temporary_manual_override and self._temporary_manual_override.active:
            self.debug_log("Clearing temporary override.")
            # Remember the cleared values: the suppress window below only mutes
            # TRV echoes that still carry THESE values — real new user input
            # (different values) must pass through immediately.
            self._cleared_override_temp = self._temporary_manual_override.target_temperature
            self._cleared_override_hvac = self._temporary_manual_override.hvac_mode
            self._temporary_manual_override = None
            # Suppress matching TRV echoes for 15s to prevent re-activation from
            # delayed TRV state-change callbacks that still carry the old
            # override temperature.
            self._override_clear_suppress_until = datetime.now(UTC) + timedelta(seconds=15)
            if trigger_update:
                self._create_tracked_task(self.async_update())
            self._create_tracked_task(self._domain_save_debouncer.async_call())

    _FINGERPRINT_PARTS = ("scheduler", "adjustment", "next_transition", "away")

    def _build_schedule_fingerprint(self, last_fp_raw: str | None) -> str:
        """Baut den Zeitplan-Fingerprint. Eigene Methode, damit der
        `away`-Guard gegen den PRODUKTIVCODE testbar ist — eine Nachbildung im
        Testfile waere nur ein Spiegel des Codes.
        """
        active_sched = self.engine.get_active_scheduler() or ""
        adj_fp = self.engine.get_adjustment_fingerprint() or ""
        
        # V1 Fix für fehlende Slot-Wechsel: Wir brauchen den Zeitfortschritt (sched_next) wieder im Fingerprint.
        # Um den bekannten Boot-Bug (False-Positive wenn Scheduler kurz unavailable ist) zu verhindern,
        # übernehmen wir den alten Wert, falls sched_next aktuell None ist.
        sched_next = self.engine.get_next_schedule_transition()
        # In HA-Zeitzone normalisieren: rohe Kalender-Kandidaten tragen den
        # Offset des Kalenders (+02:00), Redirect-Kandidaten sind UTC (+00:00).
        # Derselbe Zeitpunkt in zwei Darstellungen waere sonst ein String-
        # Unterschied ohne echte Aenderung (im Live-Log gesehen).
        sched_next_str = str(dt_util.as_local(sched_next)) if sched_next else "None"
        
        # Wenn der Fingerprint mindestens 3 Teile hat, können wir den alten sched_next Wert sicher extrahieren (Index 2).
        if sched_next is None and last_fp_raw and len(last_fp_raw.split("|")) >= 3:
            # Fallback auf den alten Wert, um False-Positives beim Booten zu vermeiden
            sched_next_str = last_fp_raw.split("|")[2]

        # Derselbe Guard wie fuer `sched_next` drei Zeilen
        # darueber, nur fuer `away`. `is_away()` ist zweiwertig und macht aus
        # jeder Unsicherheit ein `False` — ein kurz unerreichbarer Kalender,
        # Scheduler oder eine Person laesst den Fingerprint also auf "home"
        # kippen und loescht damit eine aktive Uebersteuerung, ohne dass jemand
        # nach Hause gekommen waere. Solange ein PRAESENZRELEVANTER Eingang
        # unsicher ist, gilt deshalb der letzte bekannte Wert weiter.
        #
        # Bewusst konservativ: Es wird gehalten, sobald ein solcher Eingang
        # unsicher ist — nicht erst, wenn bewiesen waere, dass genau er das
        # Ergebnis gedreht hat. Das ist dieselbe Abwaegung wie beim
        # `sched_next`-Guard, und die Alternative waere zu raten.
        away_fp = "away" if self.engine.is_away() else "home"
        if last_fp_raw:
            teile = last_fp_raw.split("|")
            # Nur pruefen, wenn der Wert ueberhaupt kippen wuerde — sonst waere
            # das Halten ein No-op, und die Unsicherheitsabfrage liefe umsonst
            # in jedem Zyklus.
            if len(teile) >= 4 and teile[3] != away_fp and self._presence_inputs_uncertain():
                # Guard-Treffer messen: Dies ist die einzige
                # Stelle, die den unterdrueckten Wechsel ueberhaupt sieht —
                # `_report_suspicious_fingerprint_change` bekommt nur den
                # fertigen Diff und kann ihn strukturell nicht rekonstruieren
                # (siehe deren Docstring). Info nur, wenn gerade eine
                # Uebersteuerung aktiv ist und der Guard sie damit ueber einen
                # echten Phasenwechsel hinausrettet; sonst debug.
                would_be, gehalten = away_fp, teile[3]
                away_fp = teile[3]
                if self._temporary_manual_override:
                    _LOGGER.info(
                        "TPX [%s]: Away-Guard haelt den Fingerprint bei '%s' statt auf "
                        "'%s' zu wechseln — ein praesenzrelevanter Eingang ist unsicher, "
                        "waehrend eine Uebersteuerung aktiv ist. Ohne den Guard waere sie "
                        "jetzt geloescht worden.",
                        self.config.name, gehalten, would_be,
                    )
                else:
                    self.debug_log(
                        f"Away-Guard haelt Fingerprint bei '{gehalten}' statt '{would_be}' "
                        "(kein aktiver Override, nur Diagnose)."
                    )

        return f"{active_sched}|{adj_fp}|{sched_next_str}|{away_fp}"

    def _schedule_comfort_signal(self) -> bool | None:
        """Rein zeitplan-getriebene Comfort-Entscheidung fuer Bedingung 3 des
        `sched_next`-Guards — ohne force_comfort/Party/Presence.

        Kalender-Modus: `is_calendar_comfort_active()`. Helper-Modus:
        `is_scheduler_active()` (enthaelt den Holiday-Zweig). Seit `sched_next`
        im Helper-Modus umleitungsabhaengig ist, haette `is_calendar_comfort_active()`
        dort immer `False` geliefert und Fall B (Toggle aus nach der rohen Grenze)
        faelschlich unterdrueckt.
        """
        if self.config.scheduling_mode == SCHEDULING_MODE_CALENDAR:
            return self.engine.is_calendar_comfort_active()
        return self.engine.is_scheduler_active()

    def _sched_next_diff_suppressible(
        self, old_fp: str, new_fp: str, current_calendar_comfort: bool | None,
    ) -> bool:
        """True, wenn eine Fingerprint-Differenz AUSSCHLIESSLICH in Komponente 3
        (`sched_next`) besteht UND diese Differenz auf eine reine
        Config-Aenderung zurueckgeht statt auf echten Zeitfortschritt.

        Hintergrund: seit `sched_next` auch von Config abhaengt
        (`vacation_home`/`*_use_day`), aendert sich sein Wert schon bei einer
        reinen Config-Aenderung, ohne dass ein echter Phasenwechsel
        stattfand — `_do_update()` wuerde einen aktiven Override sonst sofort
        loeschen, nur weil z.B. `vacation_home` umgeschaltet wurde.

        Drei Bedingungen, ALLE muessen gelten:
          1. der alte `sched_next`-Wert ist parsebar,
          2. der alte Wert liegt NACH `jetzt` (`alter > now`) — bei echtem
             Zeitfortschritt (Rueck-an-Rueck-Slots, Smart-Preconditioning-
             Grenze, oder ein Redirect-Toggle NACH der alten Grenze) gilt im
             Zyklus, der die Differenz zuerst sieht, zwangslaeufig
             `alter <= now` (sched_next aendert sich erst, sobald `now` die
             alte Grenze erreicht/ueberschritten hat) — das unterscheidet
             sauber von einer Config-Aenderung VOR der alten Grenze,
          3. das Zeitplan-Comfort-Signal (`_schedule_comfort_signal()`) ist
             zwischen letztem und aktuellem Zyklus UNVERAENDERT, beide Seiten
             nicht `None`.

        Ein naiver "halte den alten Wert, solange jetzt < alter Wert"-Guard
        (ohne Bedingung 3) wurde verworfen: er wuerde an Rueck-an-Rueck-Slots
        mit gleichbleibender Comfort-Richtung und an der Preconditioning-
        Grenze faelschlich halten, wo heute korrekt geloescht wird (durch
        Tests festgehalten).

        Bedingung 3 verglich urspruenglich `should_set_comfort()` — die VOLLE Prioritaets-
        kette (force_comfort, party, force_eco, away, Kalender, Scheduler,
        Presence, Optimum Start, siehe Docstring von `should_set_comfort()`). Ist
        z.B. `force_comfort_switch` aktiv, bleibt `should_set_comfort()`
        `True`, obwohl der Kalender-Redirect (Faelle A/B) real von True auf
        False kippt — der Guard haette Fall B faelschlich unterdrueckt, weil
        eine VOELLIG UNBETEILIGTE Prioritaetsstufe die Differenz maskiert.
        `_schedule_comfort_signal()` ist dagegen rein zeitplan-getrieben
        (keine force_comfort/party/presence-Einfluesse) und liefert je Modus
        genau das Signal, dessen Uebergaenge `sched_next` vorhersagt
        (Kalender: `is_calendar_comfort_active()`, Helper:
        `is_scheduler_active()`) — siehe Docstring von
        `_schedule_comfort_signal()`.

        Default bei Unsicherheit (Bedingung 3 nicht entscheidbar, z.B. direkt
        nach einem Neustart oder wenn `_schedule_comfort_signal()` `None`
        liefert, etwa bei kurzzeitig unerreichbarem Kalender): NICHT
        unterdruecken — bewusst asymmetrisch zum away-Guard (der bei
        Unsicherheit HAELT): ein verlorener Override ist reparabel, ein
        faelschlich gehaltener koennte eine falsche HVAC-Aktion unbegrenzt
        verlaengern.
        """
        old_parts = old_fp.split("|")
        new_parts = new_fp.split("|")
        if len(old_parts) != 4 or len(new_parts) != 4:
            return False
        if old_parts[0] != new_parts[0] or old_parts[1] != new_parts[1] or old_parts[3] != new_parts[3]:
            return False
        if old_parts[2] == new_parts[2]:
            return False

        old_sched_next_str = old_parts[2]
        if old_sched_next_str in ("None", ""):
            return False
        try:
            old_val = datetime.fromisoformat(old_sched_next_str)
        except ValueError:
            return False

        if old_val <= datetime.now(UTC):
            return False

        if self._last_comfort_decision is None or current_calendar_comfort is None:
            return False
        return self._last_comfort_decision == current_calendar_comfort

    def _presence_inputs_uncertain(self) -> bool:
        """Ist gerade ein Eingang unsicher, der `is_away()` speist?"""
        return bool(self._uncertain_presence_inputs())

    def _uncertain_presence_inputs(self) -> list[str]:
        """Die unsicheren Eingaenge, die `is_away()` speisen.

        Away-Guard und `_report_suspicious_fingerprint_change` lesen beide
        hier — so koennen ihre Mengen nicht wieder auseinanderlaufen.

        Datenquelle ist `get_uncertainty_reasons()` — seiteneffektfrei, im
        Gegensatz zu `is_anybody_home()`, das `_last_home_status` schreibt.
        Proximity kommt als `proximity:<device-id>` herein und nicht als
        `entity_id`; eine reine Mengenpruefung wuerde es wegfiltern.
        """
        relevant = {
            *self.config.persons,
            *self.config.guest_mode,
            *self.config.schedulers,
            *(self.config.calendar or ()),
        }
        presence = self.config.presence_sensor
        if isinstance(presence, list):
            relevant.update(presence)
        elif presence:
            relevant.add(presence)
        # TOTE Referenzen zaehlen hier NICHT mit. Sie sagen
        # weiterhin "ich kenne den Praesenzwert nicht" — aber sie sind eine
        # Konfigurationsstoerung, keine Laufzeitstoerung, und rechtfertigen
        # deshalb nicht den konservativen Halte-Pfad. Vorher fror ein
        # geloeschter Kalender den `away`-Teil des Fingerprints dauerhaft ein.
        #
        # Bewusst NICHT "transient" genannt: `unknown` kann ebenfalls dauerhaft
        # haengen. Massgeblich ist nicht die vermutete Dauer, sondern ob die
        # Entity ueberhaupt existiert.
        tote_referenzen = set(self.engine.get_missing_entity_references())
        return [
            e for e in self.engine.get_uncertainty_reasons()
            if (e in relevant or e.startswith("proximity:"))
            and e not in tote_referenzen
        ]

    def _report_suspicious_fingerprint_change(self, old_fp: str, new_fp: str) -> None:
        """Messung des Away-Fehlalarms — der WARNING-Zweig ist TOT.

        Ein Fingerprint-Wechsel loescht einen aktiven temporaeren Override — bei
        einem ECHTEN Phasenwechsel ist das gewollt. Der Verdacht war, dass
        er auch bei UNSICHEREN Eingaengen feuert: eine kurz
        `unavailable` Person oder ein Scheduler-Helper macht aus `is_away()`
        ein `False`, weil `None` nirgends als "weiss ich nicht" durchgereicht
        wird — `away_fp` kippt dann auf "home", ohne dass jemand nach Hause kam.

        Gemeldet wurde deshalb NUR der verdaechtige Fall: Fingerprint gewechselt,
        Override aktiv, UND mindestens ein Eingang gerade unsicher. Ein
        regulaerer Phasenwechsel mit gesunden Eingaengen blieb still — sonst
        waere die Meldung im Alltag Rauschen.

        WARUM DER WARNING-ZWEIG NICHT MEHR ERREICHBAR IST: Der `away`-Guard in
        `_build_schedule_fingerprint` und `culprits` unten lesen dieselbe
        Menge aus `_uncertain_presence_inputs()` — samt `proximity:`-
        Sonderbehandlung und ohne tote Referenzen. Wo die Warnung feuern
        wuerde, hat der Guard den `away`-Wechsel bereits unterdrueckt, also
        ist `"away" in changed` dort nie wahr. Der `debug`-Zweig unten
        protokolliert weiter und ist die einzige noch wirksame Haelfte.

        Tote Referenzen zaehlen auch hier nicht als Schuldige: Der Guard laesst
        den `away`-Wechsel bei ihnen bewusst durch, der Wechsel ist dann echt
        und kein Fehlalarm. Die tote Referenz meldet ihr eigenes Repair-Issue.

        Diese Funktion auf den Guard-TREFFER umzustellen ist strukturell
        ausgeschlossen: Diese Funktion sieht nur den fertigen Diff zwischen
        altem und neuem Fingerprint. Haelt der Guard den `away`-Teil, taucht
        "away" gar nicht in `changed` auf — es gibt hier nichts umzustellen.
        Die Guard-Treffer-Messung liegt deshalb direkt in
        `_build_schedule_fingerprint`, an der einzigen Stelle, die den
        unterdrueckten Wechsel ueberhaupt sieht (info bei aktivem Override,
        sonst debug). Offen bleibt nur noch, ob dieser WARNING-Zweig als
        stiller Netz bleibt oder entfernt gehoert.

        `get_uncertainty_reasons()` fuehrt Proximity als `proximity:<device-id>`;
        die Device-ID selbst steht in keinem Snapshot.
        """
        if not self._temporary_manual_override:
            return
        uncertain = self.engine.get_uncertainty_reasons()
        if not uncertain:
            return

        old_parts = old_fp.split("|")
        new_parts = new_fp.split("|")
        changed = [
            name
            for name, old, new in zip(self._FINGERPRINT_PARTS, old_parts, new_parts)
            if old != new
        ] or ["<unbekannt>"]

        # KORRELATION ist nicht KAUSALITAET. Wechselt der Fingerprint regulaer
        # (etwa `next_transition` am Slot-Ende) und ist zufaellig ein
        # Fenstersensor unavailable, hat die Unsicherheit mit dem Override-Clear
        # nichts zu tun. Solche Treffer als WARNING zu melden macht die Messung
        # binnen Wochen unglaubwuerdig. WARNING gibt es deshalb nur fuer die
        # Ausgangshypothese woertlich: der `away`-Teil hat gewechselt UND ein
        # PRAESENZRELEVANTER Eingang ist unsicher. Alles andere bleibt `debug` —
        # erfasst, aber leise.
        culprits = self._uncertain_presence_inputs()
        if "away" in changed and culprits:
            _LOGGER.warning(
                "TPX [%s]: Verdacht auf Away-Fehlalarm — der Away-Teil des Zeitplan-Fingerprints hat "
                "gewechselt, waehrend ein Override aktiv ist UND diese "
                "Praesenz-Eingaenge unsicher sind: %s. Der Override wird dadurch "
                "geloescht, obwohl niemand nach Hause gekommen sein muss — das "
                "ist der gesuchte Fehlalarm. Alt: %s | Neu: %s",
                self.config.name, ", ".join(culprits[:4]), old_fp, new_fp,
            )
            return

        self.debug_log(
            f"Fingerprint-Wechsel bei aktivem Override (nicht kausal): Teil(e) {', '.join(changed)} gewechselt "
            f"bei aktivem Override; unsicher waren {', '.join(uncertain[:4])}"
        )

    def _increment_schedule_revision(self) -> None:
        """The ONLY allowed mutation path for schedule revisions."""
        self._schedule_revision += 1
        self.debug_log(f"Schedule revision incremented to {self._schedule_revision}")
        
        # Temporary overrides die when the schedule changes logically.
        # In-Cycle (gerufen aus `_do_update`): kein zweiter Zyklus — der
        # laufende rechnet unmittelbar danach neu.
        self.clear_temporary_manual_override(trigger_update=False)

    async def async_unload(self) -> None:
        """Clean up listeners and timers."""
        # ZUERST das Tor zu.
        # `_updates_enabled` wird an allen Eingaengen geprueft (`async_update`,
        # die Kalender-/Zeitplan-Timer, `_on_state_change`) — danach startet kein
        # neuer Zyklus mehr, und ein TRV-Echo laeuft nicht mehr in `confirm()`
        # plus `_ledger_save_debouncer.async_call()`. Beides wuerde den Ledger
        # NACH seinem Flush (unten) noch mutieren, und der Debouncer schluckt
        # einen Save nach seinem Shutdown STILL — HA setzt `_shutdown_requested`
        # und steigt ohne Log aus. Der Delta verschwaende also lautlos, und nach
        # dem naechsten Start rechnet die Override-Erkennung auf einem veralteten "zuletzt
        # gesendet": ein Geraet, das inzwischen anders steht, gilt als manuell
        # verstellt → falscher Override.
        #
        # Post-Flip kommt hinzu, dass ein solcher Zyklus in `_drive_v2_if_active`
        # laeuft und ECHTE Service-Calls absetzt, waehrend die Integration
        # abgebaut wird. Pre-Flip landete er in V1s Apply-Schleife.
        self._updates_enabled = False

        # Einen bereits LAUFENDEN Zyklus auslaufen lassen, bevor geflusht wird —
        # das Flag greift nur an den Eingaengen, nicht mitten in `_do_update`.
        # `_update_lock` umschliesst den kompletten Zyklus (`async_update`), es
        # gibt keinen lock-freien Zweitpfad.
        #
        # BEWUSSTE GRENZE, 5 s: deckt den gesunden Fall (Sekunden). Haengt die
        # Hardware, haelt ein Zyklus MINUTEN — `action_delay` (Default 2 s) je
        # Geraet plus `safe_service_call` mit 30 s Timeout und 2 Retries (2/4 s
        # Backoff), also ~96 s pro Aufruf. Dann laufen wir in den Timeout und es
        # bleibt beim alten Verhalten. Das ist gewollt: ein minutenlang
        # blockierter Reload waere schlimmer als ein verlorener Ledger-Delta.
        # Der haengende Fall ist damit NICHT geloest, nur sichtbar gemacht.
        #
        # Teilweise geloest: der Dispatcher bekommt `should_abort=not
        # _updates_enabled` (Konstruktion in `__init__`) und beendet seine
        # Sendeschleifen an der naechsten GERAETE-Grenze — ein Mehrgeraete-Zyklus
        # (Geraete x `action_delay`) sendet nach dem Tor-Schliessen also nicht
        # mehr weiter. NICHT geloest: ein Geraet in Flight (haengende Hardware)
        # und ein einzelnes Geraet, dessen Zyklus (`action_delay` + Service-
        # Latenz) die 5 s ueberschreitet — der Hook greift nur ZWISCHEN Geraeten,
        # dort bindet weiter nur der 5-s-Timeout. Bewusst kein Abbruch zwischen Mode- und
        # Temp-Call (Teil-Kommando ohne Ledger-Eintrag).
        try:
            await asyncio.wait_for(self._update_lock.acquire(), timeout=5)
        except (TimeoutError, asyncio.TimeoutError):
            _LOGGER.warning(
                "%s: Zyklus laeuft beim Unload nach 5 s noch — Ledger-/Circuit-"
                "Breaker-Stand kann verloren gehen (haengende Hardware?)",
                self.config.name,
            )
        else:
            self._update_lock.release()

        self.debug_log("Unloading coordinator - cleaning up listeners and timers")
        if self._paused_issue_active:
            self._clear_paused_issue()
        if self._failing_issue_active:
            self._clear_failing_issue()
        if self._calib_issue_active:
            self._clear_calibration_issue()
        if self._trv_issue_active:
            self._clear_trv_issue()
        if self._degraded_issue_active:
            # Fehlte hier frueher: beim Entfernen eines Config-Entries
            # blieb `degraded_<entry_id>` sichtbar stehen, obwohl der Raum weg
            # war. HA blendet es spätestens beim nächsten Neustart aus (nicht
            # -persistente Issues laden mit `active=False`) — bis dahin steht es
            # aber im Weg, und darauf zu warten ist keine Aufraeumstrategie.
            self._clear_degraded_issue()
        if self._dead_reference_issue_active:
            self._clear_dead_reference_issue()
        if self._hysteresis_dirty:
            self._hysteresis_dirty = False
            try:
                await self._store.async_save({"outside_ok": self.engine.outside_ok})
            except Exception as exc:
                _LOGGER.warning("%s: Failed to flush hysteresis state on unload: %s", self.config.name, exc)
                
        # Flush domain state before shutting down debouncer to prevent data loss
        try:
            await self._async_save_domain_state()
        except Exception as exc:
            _LOGGER.warning("%s: Failed to flush domain state on unload: %s", self.config.name, exc)

        # Flush the V2 ledger + dispatcher circuit-breaker too.
        # Gate: V2_ACTIVE — post-flip this is V2's OWN state,
        # not shadow bookkeeping, and losing it costs a false override after the
        # next restart (the override detection needs the last commanded value).
        if V2_ACTIVE:
            try:
                await self._async_save_ledger()
            except Exception as exc:
                _LOGGER.warning("%s: Failed to flush V2 ledger on unload: %s", self.config.name, exc)
            try:
                await self._async_save_dispatcher_cb()
            except Exception as exc:
                _LOGGER.warning("%s: Failed to flush V2 dispatcher CB on unload: %s", self.config.name, exc)

        if self._save_debouncer:
            self._save_debouncer.async_shutdown()
        if self._domain_save_debouncer:
            self._domain_save_debouncer.async_shutdown()
        if self._ledger_save_debouncer:
            self._ledger_save_debouncer.async_shutdown()
        if self._dispatcher_cb_save_debouncer:
            self._dispatcher_cb_save_debouncer.async_shutdown()
        if self._calendar_report_debouncer:
            self._calendar_report_debouncer.async_shutdown()

        if self._ha_started_listener:
            self._ha_started_listener()
            self._ha_started_listener = None

        for unsub in self._listeners:
            unsub()
        self._listeners.clear()

        if self._trigger_timer:
            self._trigger_timer()
            self._trigger_timer = None

        if self._reeval_timer:
            self._reeval_timer()
            self._reeval_timer = None

        if self._v2_wakeup_timer:
            self._v2_wakeup_timer()
            self._v2_wakeup_timer = None

        for cancel_timer in self._option_timers.values():
            if cancel_timer:
                cancel_timer()
        self._option_timers.clear()

        # Cancel tracked background tasks
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()

    # ── main update loop ─────────────────────────────────────────────────────

    async def async_update(self, _now: datetime | None = None) -> None:
        """Full recalculate + apply.  Blueprint: main action block."""
        if not self._updates_enabled:
            return
        if not self._initialized:
            self.debug_log("Skipping update because domain state is still restoring.")
            return

        async with self._update_lock:
            try:
                success = await self._do_update()

                # The loop completed (True OR uncertain-False) → no crash loop.
                # Post-flip V1 is the OBSERVER — its success must NOT clear a
                # V2-raised issue. Gated; no-op pre-flip
                # (ACTIVE_ENGINE=="v1" is always true until the flip).
                if ACTIVE_ENGINE == "v1":
                    self._track_active_engine_success()

                # Loss-of-control is judged on the ACTIVE engine.
                # Pre-flip that is V1's applied status (`success`); post-flip V2
                # drives, so a V1 uncertainty (`success == False`) is NOT loss of
                # control — the room is controlled iff V2 drove a non-UNSAFE
                # decision this cycle (`_v2_controlled`). A V2 CRASH is owned by
                # the crash-loop issue, which keeps `_v2_controlled`
                # True so the paused issue does not also fire.
                controlled = (
                    self._v2_controlled if ACTIVE_ENGINE == "v2" else success
                )

                # Calibration/TRV health is V1-observation (its calibration + TRV
                # view) — gated on V1's own successful cycle, unchanged by the
                # active-engine switch above.
                if success:
                    self._track_calibration_health()
                    self._track_trv_health()

                # Bewusst NICHT an `success` gekoppelt: eine tote Referenz kann
                # die Ursache eines erfolglosen Zyklus sein.
                self._track_dead_references()

                if controlled:
                    self._set_health_clock("_uncertainty_start_time", None)
                    if self._paused_issue_active:
                        self._clear_paused_issue()
                else:
                    # Schedule a faster retry (30s) on loss of control
                    if not self._uncertainty_start_time:
                        self._set_health_clock("_uncertainty_start_time", datetime.now(UTC))

                    elapsed = (datetime.now(UTC) - self._uncertainty_start_time).total_seconds()

                    if not self._paused_issue_active and elapsed >= _CONTROL_LOST_THRESHOLD_SECONDS:
                        self._raise_paused_issue()

                    if elapsed < 300:  # 5 minutes
                        if self._reeval_timer:
                            self._reeval_timer()
                        self.debug_log(
                            f"Update paused due to uncertainty (elapsed: {elapsed:.0f}s). "
                            "Scheduling retry in 30s."
                        )
                        self._reeval_timer = async_call_later(
                            self.hass, 30, self._delayed_update
                        )
                    else:
                        self.debug_log("Uncertainty timeout (5min) reached. Falling back to default heartbeat.")

            except Exception as exc:
                _LOGGER.error("%s: update error: %s", self.config.name, exc, exc_info=True)
                # Post-flip V1 is the OBSERVER — its crash alone does not mean
                # the room is uncontrolled, as long as V2 got to drive this
                # cycle. Crashed V1 code BEFORE the V2 block means V2 never
                # ran: the room is uncontrolled, so that counts too.
                if ACTIVE_ENGINE == "v1" or not self._v2_block_reached:
                    self._track_active_engine_failure(exc)

    def _set_health_clock(self, attr: str, value: datetime | None) -> None:
        """Set one of the six health-clock start times and persist the
        transition. Before this, all six lived only in
        memory — every restart (including a crash-loop restart, the exact
        moment a stuck room is most likely overdue for a Repair) reset every
        one of them to None, silently giving each stuck room a fresh
        15-minute grace period. Debounced like every other domain-store
        write; a no-op when the value doesn't actually change, so a healthy
        room whose clock stays None every cycle triggers no writes at all.
        """
        if getattr(self, attr) == value:
            return
        setattr(self, attr, value)
        self._create_tracked_task(self._domain_save_debouncer.async_call())

    def _persist_suppressed_fingerprint(self) -> None:
        """Den fortgeschriebenen Fingerprint speichern, wenn ein unterdrueckter
        `sched_next`-Diff einen aktiven Override am Leben liess (im Live-Test
        gefunden).

        Vorher stand der Fingerprint im Store nur vom Zeitpunkt der letzten
        Setter-Aktion (Override setzen/loeschen, Timer). Der Zyklus schreibt ihn
        im Speicher jedes Mal auf den frischen Wert (richtig so, nie auf einen
        gehaltenen), speicherte aber nicht. Gemessen: Override gesetzt (Fingerprint
        10:12), `vacation_home` an -> Diff unterdrueckt (10:12 -> 10:00), HA-Restart
        -> der Override wurde restauriert und im ersten Zyklus geloescht, weil der
        gespeicherte Wert (10:12) gegen den frischen (10:00) wie ein Phasenwechsel
        aussah. Ohne vorherigen Toggle ueberlebte derselbe Override den Restart.

        Das aendert die Unterdrueckungs-Regeln NICHT: bei Unsicherheit
        (`_last_comfort_decision` ist nach dem Start None) bleibt der Default
        "nicht unterdruecken = loeschen". Es sorgt nur dafuer, dass nach dem Start
        nichts mehr gegen einen veralteten Wert verglichen wird. Ein Restart ueber
        eine ECHTE Zeitplangrenze loescht weiterhin — dort aendert sich der
        Fingerprint wirklich.

        Schreibt nur im (seltenen) unterdrueckten Fall mit aktivem Override, nie
        pro Zyklus; die Regel "kein Regelzyklus speichert pauschal" bleibt.
        """
        self._create_tracked_task(self._domain_save_debouncer.async_call())

    def _raise_failing_issue(self, exc: Exception) -> None:
        """Repair issue for a persistent crash loop.

        A ``_do_update()`` that RAISES every cycle never reaches the
        uncertainty tracking, so the paused issue stays silent — yet the room
        is just as dead. Same threshold as the paused issue
        (``_CONTROL_LOST_THRESHOLD_SECONDS``, 15 min), own
        translation key so the text says "failing", not "sensor unavailable".
        """
        self._failing_issue_active = True
        since = (
            dt_util.as_local(self._error_loop_start).strftime("%Y-%m-%d %H:%M")
            if self._error_loop_start else "?"
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"update_failing_{self.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="room_update_failing",
            translation_placeholders={
                "name": self.config.name,
                "error": f"{type(exc).__name__}: {exc}",
                "since": since,
            },
        )
        _LOGGER.warning(
            "%s: update loop failing for over %ds — Repair issue raised",
            self.config.name, _CONTROL_LOST_THRESHOLD_SECONDS,
        )

    def _clear_failing_issue(self) -> None:
        """Auto-resolve the crash-loop Repair issue once updates succeed again."""
        self._failing_issue_active = False
        ir.async_delete_issue(self.hass, DOMAIN, f"update_failing_{self.entry_id}")

    def _track_active_engine_failure(self, exc: Exception) -> None:
        """Crash-loop tracking for whichever engine DRIVES the hardware:
        start the clock on the first failure, raise the
        `update_failing` issue after the threshold. Called by V1's outer loop
        pre-flip and by V2's evaluation post-flip — never both (mutually
        exclusive on ACTIVE_ENGINE), so the shared clock is safe."""
        if self._error_loop_start is None:
            self._set_health_clock("_error_loop_start", datetime.now(UTC))
        elif (
            not self._failing_issue_active
            and (datetime.now(UTC) - self._error_loop_start).total_seconds()
            >= _CONTROL_LOST_THRESHOLD_SECONDS
        ):
            self._raise_failing_issue(exc)

    def _track_active_engine_success(self) -> None:
        """A successful ACTIVE-engine cycle → reset the crash-loop clock and
        clear the issue."""
        self._set_health_clock("_error_loop_start", None)
        if self._failing_issue_active:
            self._clear_failing_issue()

    def _should_run_v2(self, target_temp, hvac_mode) -> bool:
        """Decide whether the V2
        parallel/drive block runs this cycle.

        The observe-only structure gated the whole V2 block (incl.
        ``_drive_v2_if_active``) behind V1 having a certain target — V1's chains
        return None on uncertainty, so post-flip that would freeze the room even
        though V2 has a safe decision (V2 never blindly freezes).

        - Never when the room is paused (``manual_mode``) — hands-off for
          both engines — or when the V2 stack is off entirely (``V2_ACTIVE``).
        - POST-FLIP (``ACTIVE_ENGINE == "v2"``): ALWAYS run, so V2 drives
          independently of V1's certainty.
        - PRE-FLIP: only when V1 is certain (there is a target to diff against).
          Unchanged — the flip is the single behaviour change.

        This gate used to read ``not PARALLEL_VALIDATION``.
        That made the drive depend on the OBSERVER flag — and since V1's apply
        loop is gated off post-flip, the combination ("v2", False) left every
        room silently unregulated: no send, no exception, and no repair issue
        (``_v2_controlled`` defaults to True). ``V2_ACTIVE`` is true whenever V2
        drives, so the combination can no longer disable the engine.
        """
        if not V2_ACTIVE or self.config.manual_mode:
            return False
        if ACTIVE_ENGINE == "v2":
            return True
        return target_temp is not None and hvac_mode is not None

    async def _drive_v2_if_active(self, v2_result) -> None:
        """Post-flip, send the commands V2 just planned over the
        long-lived dispatcher — those EXACT commands, never re-planned (run()
        already advanced the calibration deadband once). Gated on the flag +
        UNSAFE-guarded (hardware safeguard, same as ``dispatch()``); a no-op
        pre-flip. Extracted from the parallel-validation block so the drive path
        is directly testable — that block is mocked away in the coordinator
        tests (same reason the crash-loop helpers are extracted)."""
        if ACTIVE_ENGINE != "v2" or v2_result.validity is SnapshotValidity.UNSAFE:
            return
        # Echo-Buchhaltung: siehe `_track_v2_send` (on_send-Hook).
        #
        # Getrackt wird ueber den `on_send`-Hook des Dispatchers, NICHT hier:
        # `hardware_commands` ist der PLAN, und der Dispatcher ueberspringt
        # daraus noch Geraete (Repeat-Guard, min_delta, CB-Backoff, nicht
        # unterstuetzter Modus ohne `off`) bzw. schreibt `dry` auf `off` um.
        # Wer den Plan trackt, legt fuer jedes uebersprungene Geraet einen
        # Phantom-Eintrag in `_pending_commands` an — dreht der Nutzer innerhalb
        # der TTL genau auf diesen Wert, gilt sein Griff als Selbst-Echo und der
        # Override entfaellt. Das ist dieselbe Bug-Klasse wie der falsche
        # Override durch das eigene Echo, nur spiegelverkehrt
        # (gefunden im Code-Vergleich mit V1, das erst NACH seiner
        # CB-Pruefung trackt).
        #
        # Der Hook feuert unmittelbar VOR dem Service-Call, wie V1 es auch tut
        # (track dann senden): das Echo kann schneller zurueckkommen als der
        # Sende-Await zurueckkehrt. Ein Eintrag ohne erfolgreichen Send
        # verfaellt ueber die TTL (`_prune_expired_commands`) — V1-Verhalten.
        #
        # Gleiche Form wie V1s `changes` — Sensor, Climate-Karte und Diagnose
        # lesen daraus `entity_id`/`temperature`. Post-Flip stand dort V1s
        # SCHATTEN-Berechnung, also was V1 senden wuerde, nicht was V2 plante.
        #
        # `temperature` wird WEGGELASSEN statt auf None gesetzt, wenn das
        # Kommando keine traegt (off, dry/fan_only) — genau wie V1 den Key nur
        # bei `valve_mode != "off"` setzt (engine_calibration.py). Die Leser
        # filtern per `if "temperature" in c` bzw. `.get("temperature")`; mit
        # einem None-Wert rutschten ausgeschaltete Ventile als
        # `{"climate.trv": None}` ins `valve_targets`-Attribut.
        #
        # Das ist BEWUSST der Plan und nicht die Dispatch-Bilanz.
        # `valve_targets` auf der Karte beantwortet "worauf steht dieses Ventil",
        # nicht "was ging in diesem Zyklus raus". Wuerde hier nur der echte Send
        # stehen, verschwaende ein per Repeat-Guard unterdruecktes TRV aus der
        # Anzeige, obwohl es genau auf dem Wert steht — die Anzeige flackerte im
        # Zyklustakt. V1s `changes` ist aus demselben Grund planner-abgeleitet;
        # die Paritaet stimmt also. Wer die tatsaechlichen Sends braucht, nimmt
        # `_last_dispatch` (Diagnose) oder das `DispatchSummary` direkt.
        self._v2_sent_changes = [
            {
                "entity_id": c.entity_id,
                "hvac_mode": c.hvac_mode,
                **({"temperature": c.target_temperature}
                   if c.target_temperature is not None else {}),
            }
            for c in v2_result.hardware_commands
        ]
        summary = await self._v2_dispatcher.dispatch_commands(
            v2_result.hardware_commands,
        )
        # Was der Dispatcher wirklich getan hat — pro Geraet, inklusive der
        # Unterdrueckungen, die im Plan oben unsichtbar sind (als
        # `result: "skipped"` mit Grund).
        # Nur fuer die Diagnose; kein Entity-Attribut haengt daran, also darf
        # es flackern.
        self._last_dispatch = [
            {
                "entity_id": r.entity_id,
                "result": r.result.value,
                "detail": r.detail,
                "sent": r.command is not None,
            }
            for r in summary.results
        ]
        # Entity-based calibration + valve positioning are separate
        # command kinds — the dispatcher owns their send paths (number.set_value
        # / Tado offset), the validator planned them onto the result. Best-effort,
        # in V1's apply-loop order (climate → calibration → valve).
        #
        # V1's window guard (in its apply loop): when the calibration block is
        # active it re-checks the window right before calibrating and returns —
        # skipping BOTH calibration and valve — if a window opened mid-cycle
        # (don't recalibrate/reposition against a window-open scene). Replicated
        # faithfully so the post-flip send matches V1. (When the block is
        # inactive, V1 runs valve
        # regardless of the window, so the guard is gated on the block too.)
        calibration_block_active = (
            self.config.calibration_mode != CALIBRATION_MODE_OFF
            or self.config.aggressive_mode_selector == AGGRESSIVE_MODE_CALIBRATION
        )
        if calibration_block_active and self.engine.is_window_open():
            return
        # Rueckgabe = die TATSAECHLICH geschriebenen Offsets (gedrosselte
        # fehlen), gleiche Semantik wie V1s `CalibrationApplier.apply()`.
        # Ohne dieses Update fror `last_calibrations` post-Flip auf dem Stand
        # vor dem Flip ein — Sensor, Climate-Karte und Diagnose haetten im
        # 7-Tage-Fenster veraltete Kalibrierwerte gezeigt. Dritter Fall
        # derselben Klasse nach `_pending_commands` und `last_changes`.
        new_cals = await self._v2_dispatcher.dispatch_calibrations(
            v2_result.calibration_commands, datetime.now(UTC))
        if new_cals:
            self.last_calibrations.update(new_cals)
        await self._v2_dispatcher.dispatch_valves(v2_result.valve_commands)

    # V2-Goal → V1-Status-Vokabular. Nur die Goals mit
    # 1:1-Entsprechung; FORCE und SCHEDULE spalten sich am Comfort-Zweig und
    # werden in `_v2_climate_state` aufgeloest.
    _V2_GOAL_STATES: dict[GoalClass, ClimateState] = {
        GoalClass.SYSTEM: ClimateState.INACTIVE,
        GoalClass.FROST: ClimateState.FROST_PROTECTION,
        GoalClass.MANUAL_OVERRIDE: ClimateState.TEMPORARY_MANUAL_OVERRIDE,
        GoalClass.LIMING: ClimateState.LIMING,
        GoalClass.VACATION: ClimateState.VACATION,
        GoalClass.PARTY: ClimateState.PARTY,
        GoalClass.ADJUSTMENT: ClimateState.ADJUSTMENT,
    }

    def _v2_climate_state(self, v2_result, set_comfort: bool | None) -> ClimateState:
        """V2s Entscheidung im Status-Vokabular von V1.

        Abgeleitet aus GENAU den Fakten, die V2 selbst benutzt hat: dem
        siegreichen Goal, dem Constraint-Trace (der benennt, welcher Constraint
        wirklich eingegriffen hat) und den gebrueckten Resolvern, die den
        Snapshot gefuellt haben. Hier wird nichts neu entschieden — das ist eine
        Darstellung von V2s Ergebnis, keine zweite Meinung.
        """
        if self.config.manual_mode:
            return ClimateState.MANUAL_MODE
        # UNSAFE loest die Hardware-Sicherung aus — es ging nichts raus, der
        # Raum ist also tatsaechlich ungeregelt und "Paused" ist ehrlich.
        if v2_result.validity is SnapshotValidity.UNSAFE:
            return ClimateState.PAUSED

        # Die Reducer laufen NACH der Goal-Auswahl und koennen das Ziel
        # uebernehmen, ohne das Goal zu aendern — der Trace haelt fest, welcher.
        fired = {entry.policy for entry in v2_result.trace.entries}
        if "FrostConstraint" in fired:
            return ClimateState.FROST_PROTECTION
        if "WindowConstraint" in fired:
            return ClimateState.WINDOW_OPEN

        # Liest den Snapshot statt
        # live engine.*() — force_comfort/away sind selbst noch V1-gebrueckt
        # (spart nur die doppelte Berechnung im selben Zyklus, bei away
        # zusaetzlich einen zweiten is_anybody_home()-Seiteneffekt),
        # preconditioning_active ist ein echter Gewinn: is_smart_
        # preconditioning_active() las bisher ein Instanz-Flag, das als
        # Seiteneffekt von should_set_comfort() in _do_update() gesetzt wird
        # (CQS-Kopplung) — resolve_schedule_comfort() liefert denselben Wert
        # nativ als Rueckgabewert, ohne diese Kopplung.
        snapshot = self._v2_last_snapshot
        mapped = self._V2_GOAL_STATES.get(v2_result.goal_class)
        if mapped is not None:
            return mapped
        if v2_result.goal_class is GoalClass.FORCE:
            # ForceGoal: Comfort schlaegt Eco (V1-Reihenfolge, goals.py).
            return (
                ClimateState.FORCE_COMFORT if bool(snapshot and snapshot.force_comfort)
                else ClimateState.FORCE_ECO
            )

        # SCHEDULE (das Fallback-Goal) — dieselbe Zweig-Regel wie SchedulePolicy
        # (`schedule_comfort is True or preconditioning_active`).
        if bool(snapshot and snapshot.preconditioning_active):
            return ClimateState.SMART_PRECONDITIONING
        if set_comfort is not True:
            return ClimateState.ECO
        return ClimateState.AWAY if bool(snapshot and snapshot.away) else ClimateState.COMFORT

    def _apply_v2_status(self, v2_result, set_comfort: bool | None) -> None:
        """Post-Flip die oeffentliche Raum-Anzeige auf V2 umstellen.

        `current_hvac` / `current_temperature` / `current_state` /
        `current_reason` stammten frueher ausschliesslich aus V1s Kette
        (weiter oben in `_do_update` gesetzt) — post-Flip also aus dem
        BEOBACHTER. Dieselbe Klasse wie `last_changes`, `last_calibrations` und
        `_pending_commands` davor, nur mit zwei zusaetzlichen Folgen:

        * Ist V1 unsicher (`target_temp is None`, z.B. Fenstersensor
          unavailable, wo V2 mit einer Fenster-Annahme weiterfaehrt), meldete
          die Karte `off` und der
          Statussensor "Paused", waehrend V2 den Raum aktiv fuhr.
        * `TempixOverrideSwitch.async_turn_on` friert diese Werte als Override
          ein. Es wurde also V1s Wert festgehalten, den der Raum nie gefahren
          hat (dort, wo V1 und V2 bewusst verschieden entscheiden) — bzw. bei
          V1-Unsicherheit gar keiner.

        V1s Werte bleiben bis hierher stehen: der Paritaets-Diff braucht sie als
        `v1_state_value`, und ein Fehler in `validator.run` (aeusserer
        except-Zweig) laesst damit automatisch V1s Anzeige stehen.

        Das laeuft VOR dem Senden. Wirft danach der Drive
        unerwartet, steht V2s Entscheidung trotzdem in der Anzeige — wie schon
        bei abgefangenen Geraetefehlern (FAILED im Dispatcher): angezeigt wird,
        was V2 entschieden hat, nicht, ob jedes Geraet es angenommen hat.
        """
        if ACTIVE_ENGINE != "v2":
            return
        self.current_hvac = v2_result.target_state.hvac_mode
        self.current_temperature = v2_result.target_state.target_temperature
        self.current_state = self._v2_climate_state(v2_result, set_comfort)
        self.current_reason = self._build_reason(set_comfort)

    def _raise_paused_issue(self) -> None:
        """Create a Repair issue once a room has been stuck PAUSED/uncertain
        for longer than ``_CONTROL_LOST_THRESHOLD_SECONDS``.

        Without this, a dead/unavailable sensor silently blocks climate
        control for hours with zero user-facing signal — the live status
        sensor only shows the *current* reason and forgets it the moment the
        sensor recovers (real incident: window sensor unavailable 09:12-?,
        AC never ran, nothing visible afterwards).
        """
        self._paused_issue_active = True
        reasons = self.engine.get_uncertainty_reasons()
        reason_text = ", ".join(reasons) if reasons else "unknown"
        since = (
            dt_util.as_local(self._uncertainty_start_time).strftime("%Y-%m-%d %H:%M")
            if self._uncertainty_start_time else "?"
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"paused_{self.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="room_paused",
            translation_placeholders={
                "name": self.config.name,
                "reason": reason_text,
                "since": since,
            },
        )
        _LOGGER.warning(
            "%s: paused/uncertain for over %ds (reason: %s) — Repair issue raised",
            self.config.name, _CONTROL_LOST_THRESHOLD_SECONDS, reason_text,
        )

    def _clear_paused_issue(self) -> None:
        """Auto-resolve the paused Repair issue once the room recovers."""
        self._paused_issue_active = False
        ir.async_delete_issue(self.hass, DOMAIN, f"paused_{self.entry_id}")

    def _no_actuator_reachable(self) -> bool:
        """Kein einziger konfigurierter Aktor ist erreichbar.

        Gleicher Verfuegbarkeitstest wie ``_track_trv_health`` (INVALID_STATES),
        aber bewusst ueber ALLE Geraete statt ueber eines: faellt in einem Raum
        mit zwei Ventilen nur eines aus, regelt das andere weiter — dort ist der
        Ersatzwert-Hinweis echte Information und darf nicht verschwinden.
        """
        trvs = self.config.trvs
        if not trvs:
            return False
        for trv_id in trvs:
            state = self.hass.states.get(trv_id)
            if state is not None and state.state not in INVALID_STATES:
                return False
        return True

    @staticmethod
    def _room_idle_by_design(v2_result) -> bool:
        """Der Raum wird gerade bewusst nicht geregelt.

        ``GoalClass.SYSTEM`` = Master-Gate zu (Automatik aus, Saison aus,
        Aussentemperatur-Sperre) und Ziel ``off``. Bewusst eng: mit
        ``idle_temperature > 0`` bleibt SYSTEM temperaturgesteuert, und greift
        der ``FrostConstraint`` ein, haengt der Schutz am Raumsensor — in
        beiden Faellen bleibt die Warnung stehen.
        """
        if v2_result.goal_class is not GoalClass.SYSTEM:
            return False
        if not v2_result.target_state.is_off:
            return False
        return all(
            entry.policy != "FrostConstraint" for entry in v2_result.trace.entries
        )

    def _track_validity_degraded(self, v2_result) -> None:
        """Track a sustained DEGRADED decision. ``_v2_controlled`` only
        reacts to UNSAFE, so a
        room stuck DEGRADED — external room sensor down and falling back to
        the TRV's own sensor, an unresolved schedule, or any future
        DEGRADED source — drives on a compromised input indefinitely with no
        more signal than a DEBUG log line. This is the missing counterpart,
        generic over ``validity_reasons`` so no cause needs its own tracker.
        Caller gates this to post-flip: pre-flip V2 is a dry-run observer,
        so "degraded" would describe nothing actually driving hardware."""
        if v2_result.validity is not SnapshotValidity.DEGRADED:
            self._set_health_clock("_degraded_start_time", None)
            if self._degraded_issue_active:
                self._clear_degraded_issue()
            return

        if self._no_actuator_reachable():
            # Doppelmeldung vermeiden: steht kein Aktor mehr zur
            # Verfuegung, wird der Raum gar nicht geregelt — "laeuft mit einem
            # Ersatzwert" beschriebe dann eine Regelung, die nicht stattfindet,
            # und lenkt auf den Sensor statt auf das tote Geraet. Das
            # `trv_unavailable`-Issue (ERROR) benennt die Ursache ohnehin
            # praeziser. Gleiche Ueberlegung wie bei der Kalibrier-Meldung, die
            # aus demselben Grund auf "heizt/kuehlt aktiv" eingeschraenkt ist:
            # eine Dauerwarnung ohne Handlungsmoeglichkeit trainiert nur das
            # Wegklicken an.
            self._set_health_clock("_degraded_start_time", None)
            if self._degraded_issue_active:
                self._clear_degraded_issue()
            return

        if self._room_idle_by_design(v2_result):
            # Raum wird bewusst nicht geregelt: Automatik aus /
            # Saison aus / Aussentemperatur-Sperre und nichts, das am
            # Raumsensor haengt. Ein eingefrorener Sensor kann dann nichts
            # verfaelschen — die Meldung waere eine Dauerwarnung ohne
            # Handlungsmoeglichkeit (live: ein Raum, "Season Off", Sensor
            # 26 h auf 17,5 °C im 0,5-°C-Raster). Beginnt die Regelung wieder,
            # laeuft die Uhr neu an und die Meldung kommt nach 15 min zurueck,
            # falls der Sensor dann noch eingefroren ist.
            self._set_health_clock("_degraded_start_time", None)
            if self._degraded_issue_active:
                self._clear_degraded_issue()
            return

        if self._degraded_start_time is None:
            self._set_health_clock("_degraded_start_time", datetime.now(UTC))
            return

        elapsed = (datetime.now(UTC) - self._degraded_start_time).total_seconds()
        if not self._degraded_issue_active and elapsed >= _CONTROL_LOST_THRESHOLD_SECONDS:
            self._raise_degraded_issue(v2_result.validity_reasons)

    def _raise_degraded_issue(self, reasons: tuple[str, ...]) -> None:
        """Repair issue: the room has been on a DEGRADED snapshot (fallback
        value substituted for the real input) for over
        ``_CONTROL_LOST_THRESHOLD_SECONDS`` — same threshold as the paused
        issue, since both describe "the input can no longer be trusted",
        just at different severities (UNSAFE vs. DEGRADED)."""
        self._degraded_issue_active = True
        reason_text = ", ".join(reasons) if reasons else "unknown"
        since = (
            dt_util.as_local(self._degraded_start_time).strftime("%Y-%m-%d %H:%M")
            if self._degraded_start_time else "?"
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"degraded_{self.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="room_degraded",
            translation_placeholders={
                "name": self.config.name,
                "reason": reason_text,
                "since": since,
            },
        )
        _LOGGER.warning(
            "%s: running on a degraded input for over %ds (reason: %s) — Repair issue raised",
            self.config.name, _CONTROL_LOST_THRESHOLD_SECONDS, reason_text,
        )

    def _clear_degraded_issue(self) -> None:
        """Auto-resolve once the snapshot leaves DEGRADED (back to VALID, or
        escalated to UNSAFE — the paused issue's territory from there)."""
        self._degraded_issue_active = False
        ir.async_delete_issue(self.hass, DOMAIN, f"degraded_{self.entry_id}")

    def _track_dead_references(self) -> None:
        """Melde konfigurierte Entities, die es nicht (mehr) gibt.

        **Warum es das braucht.** Eine tote Referenz erzeugt keinen Fehler. Sie
        wirkt still: ein geloeschter Kalender hat so schon in mehreren
        Raeumen `is_away()` dauerhaft auf `False` genagelt und damit die
        Abwesenheitserkennung abgeschaltet — ohne Log, ohne Repair, ohne
        Anzeige. Aufgefallen ist es, weil der Nutzer das Symptom meldete. Die
        Laufzeit-Repairs decken Laufzeitstoerungen ab; dieses deckt die
        Konfigurationsstoerung.

        **Die Schwelle ist reine Melde-Politik, keine Klassifikation.**
        `_entity_is_missing()` entscheidet kausal ueber die Entity-Registry und
        gilt sofort. Gewartet wird nur, um
        den Nutzer nicht zu behelligen: Entities ohne `unique_id` (reine
        YAML-Helfer) stehen nie in der Registry und koennen waehrend des Starts
        kurz als tot gelten. Nach ``_CONTROL_LOST_THRESHOLD_SECONDS`` ist das
        vorbei — dieselbe Schwelle wie bei den uebrigen Repairs.

        **Nicht an `success` gekoppelt**, anders als die Kalibrier- und
        TRV-Tracker: eine tote Referenz kann die URSACHE eines erfolglosen
        Zyklus sein (eine geloeschte Person macht den Zyklus unsicher). Haenge
        man die Meldung an den Erfolg, bliebe sie genau dann stumm, wenn sie
        gebraucht wird.

        Ein Snapshot-Read ist hier NICHT sicher (anders als bei
        `_build_reason`) — waehrend `manual_mode` aktiv ist, laeuft
        `_should_run_v2()` gar nicht mehr, `_v2_last_snapshot` friert fuer die
        GANZE Pausendauer ein. Genau dann darf diese Methode aber nicht
        stumm werden (siehe Docstring oben, "nicht an success gekoppelt") —
        ein waehrend der Pause geloeschter Verweis wuerde sonst unbegrenzt
        lange nicht gemeldet. Bleibt deshalb bewusst auf dem live Engine-Call,
        nicht auf `resolve_missing_entity_references()` migriert.
        Nachtrag 2026-11-10: Der Pausenzweig baut den Snapshot inzwischen
        jeden Zyklus neu (`_refresh_paused_snapshot`), das Einfrieren ist
        damit behoben. Eine Migration hier waere trotzdem neu zu pruefen
        (Reihenfolge im Zyklus, Fehlerpfad des Refresh) — nicht gemacht.
        """
        tot = tuple(sorted(self.engine.get_missing_entity_references()))

        if not tot:
            self._set_health_clock("_dead_reference_start_time", None)
            if self._dead_reference_issue_active:
                self._clear_dead_reference_issue()
            return

        if self._dead_reference_start_time is None:
            self._set_health_clock("_dead_reference_start_time", datetime.now(UTC))
            return

        elapsed = (datetime.now(UTC) - self._dead_reference_start_time).total_seconds()
        if elapsed < _CONTROL_LOST_THRESHOLD_SECONDS:
            return

        # Auch neu melden, wenn sich die MENGE geaendert hat — sonst nennt der
        # Text weiter nur die zuerst gefundene Entity, waehrend inzwischen eine
        # zweite fehlt. `async_create_issue` aktualisiert bei gleicher ID.
        if not self._dead_reference_issue_active or tot != self._dead_reference_reported:
            self._raise_dead_reference_issue(tot)

    def _raise_dead_reference_issue(self, tot: tuple[str, ...]) -> None:
        """Repair issue: konfigurierte Entities existieren nicht (mehr).

        Bewusst anders formuliert als die Repairs zu nicht verfuegbaren Eingaengen
        (z. B. ``room_trv_unavailable``): die versprechen
        „loest sich selbst auf, sobald der Eingang wieder gueltig ist". Das
        stimmt hier NICHT — eine geloeschte Entity kommt nicht von allein
        zurueck. Der Text verlangt deshalb eine Handlung.
        """
        self._dead_reference_issue_active = True
        self._dead_reference_reported = tot
        since = (
            dt_util.as_local(self._dead_reference_start_time).strftime("%Y-%m-%d %H:%M")
            if self._dead_reference_start_time else "?"
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"dead_reference_{self.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="room_dead_reference",
            translation_placeholders={
                "name": self.config.name,
                "entities": ", ".join(tot),
                "since": since,
            },
        )
        _LOGGER.warning(
            "%s: configured entities do not exist (%s) — Repair issue raised",
            self.config.name, ", ".join(tot),
        )

    def _clear_dead_reference_issue(self) -> None:
        """Aufloesen, sobald keine Referenz mehr tot ist — also wenn der Nutzer
        sie aus der Konfiguration entfernt oder die Entity wiederhergestellt
        hat."""
        self._dead_reference_issue_active = False
        self._dead_reference_reported = ()
        ir.async_delete_issue(self.hass, DOMAIN, f"dead_reference_{self.entry_id}")

    def _track_calibration_health(self) -> None:
        """Track generic calibration silently degrading.

        Generic calibration is skipped per-valve when the TRV reports no
        internal temperature (``current_temperature: None``) — the entity
        stays valid, the cycle succeeds, nothing surfaces. The clock only
        runs while the room actively heats/cools: an idle TRV (FRITZ summer
        mode) is uncalibrated but the setpoint never reaches the hardware,
        so a standing warning would only train the user to ignore Repairs.
        """
        if (
            self.config.calibration_mode != CALIBRATION_MODE_GENERIC
            or self.current_hvac in (None, "off")
        ):
            self._set_health_clock("_calib_missing_start", None)
            if self._calib_issue_active:
                self._clear_calibration_issue()
            return

        affected = []
        for trv_id in self.config.trvs:
            state = self.hass.states.get(trv_id)
            if (
                state is not None
                and state.state not in INVALID_STATES
                and state.attributes.get("current_temperature") is None
            ):
                affected.append(trv_id)

        if not affected:
            self._set_health_clock("_calib_missing_start", None)
            if self._calib_issue_active:
                self._clear_calibration_issue()
            return

        if self._calib_missing_start is None:
            self._set_health_clock("_calib_missing_start", datetime.now(UTC))
            return

        if (
            not self._calib_issue_active
            and (datetime.now(UTC) - self._calib_missing_start).total_seconds()
            >= _CALIBRATION_ISSUE_THRESHOLD_SECONDS
        ):
            self._raise_calibration_issue(affected)

    def _raise_calibration_issue(self, affected: list[str]) -> None:
        """Repair issue: generic calibration inactive while actively heating/cooling."""
        self._calib_issue_active = True
        since = (
            dt_util.as_local(self._calib_missing_start).strftime("%Y-%m-%d %H:%M")
            if self._calib_missing_start else "?"
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"calibration_unavailable_{self.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="room_calibration_unavailable",
            translation_placeholders={
                "name": self.config.name,
                "trvs": ", ".join(affected),
                "since": since,
            },
        )
        _LOGGER.warning(
            "%s: generic calibration inactive for over %ds while heating/cooling "
            "(no internal temperature from: %s) — Repair issue raised",
            self.config.name, _CALIBRATION_ISSUE_THRESHOLD_SECONDS, ", ".join(affected),
        )

    def _clear_calibration_issue(self) -> None:
        """Auto-resolve once the TRV reports an internal temperature again."""
        self._calib_issue_active = False
        ir.async_delete_issue(self.hass, DOMAIN, f"calibration_unavailable_{self.entry_id}")

    def _track_trv_health(self) -> None:
        """Track a TRV/climate actuator being unavailable.

        ``calculate_changes`` skips TRVs whose entity state is unavailable or
        unknown (INVALID_STATES) — no command is generated, so the circuit
        breaker (which only trips on *failed sends*) never engages. The cycle
        "succeeds", the status sensor still shows a reason, and the valve is
        driven by nobody. Unlike calibration this is NOT gated on hvac: a dead
        actuator matters even at rest (it may be stuck heating).

        Also tracked during a deliberate pause (``manual_mode``): the Tempix card stays the room's remote in passthrough and
        updates optimistically, so a dead device looks like a working one.
        There it is a WARNING with its own ID — Tempix drives nothing, and an
        "ignore" on the paused issue must not swallow the ERROR once the pause
        ends (HA keeps ``dismissed_version`` when an issue ID is recreated).
        A mode switch swaps the issue; the clock keeps running.
        """
        affected = []
        for trv_id in self.config.trvs:
            state = self.hass.states.get(trv_id)
            if state is None or state.state in INVALID_STATES:
                affected.append(trv_id)

        if not affected:
            # Also clear when only the clock says a device was down: after a
            # restart `_trv_issue_active` starts False, but HA reloaded the
            # issue (inactive) together with its "ignored" flag — left there,
            # it would silently ignore the next outage too.
            was_missing = self._trv_missing_start is not None
            self._set_health_clock("_trv_missing_start", None)
            if self._trv_issue_active or was_missing:
                self._clear_trv_issue()
            return

        if self._trv_missing_start is None:
            self._set_health_clock("_trv_missing_start", datetime.now(UTC))
            return

        if (
            datetime.now(UTC) - self._trv_missing_start
        ).total_seconds() < _CONTROL_LOST_THRESHOLD_SECONDS:
            return

        paused = bool(self.config.manual_mode)
        if self._trv_issue_active:
            if self._trv_issue_paused == paused:
                return
            self._clear_trv_issue()
        self._raise_trv_issue(affected, paused=paused)

    def _raise_trv_issue(self, affected: list[str], *, paused: bool = False) -> None:
        """Repair issue: a thermostat/AC actuator is unavailable."""
        self._trv_issue_active = True
        self._trv_issue_paused = paused
        since = (
            dt_util.as_local(self._trv_missing_start).strftime("%Y-%m-%d %H:%M")
            if self._trv_missing_start else "?"
        )
        issue_id = (f"trv_unavailable_paused_{self.entry_id}" if paused
                    else f"trv_unavailable_{self.entry_id}")
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=(ir.IssueSeverity.WARNING if paused
                      else ir.IssueSeverity.ERROR),
            translation_key=("room_trv_unavailable_paused" if paused
                             else "room_trv_unavailable"),
            translation_placeholders={
                "name": self.config.name,
                "trvs": ", ".join(affected),
                "since": since,
            },
        )
        # HA keeps the "ignored" flag across restarts, and after one the issue
        # is re-raised here (``_trv_issue_active`` starts False). The user
        # already decided — don't repeat the WARNING on every restart. A
        # fresh issue (recovery or kind switch deleted the old one) is not
        # ignored and still warns.
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id)
        ignored = issue is not None and issue.dismissed_version is not None
        _LOGGER.log(
            logging.DEBUG if ignored else logging.WARNING,
            "%s: actuator unavailable for over %ds (%s)%s — Repair issue raised",
            self.config.name, _CONTROL_LOST_THRESHOLD_SECONDS, ", ".join(affected),
            " during manual mode" if paused else "",
        )

    def _clear_trv_issue(self) -> None:
        """Auto-resolve once every TRV is reachable again. Deletes both IDs —
        a no-op in HA for one that does not exist."""
        self._trv_issue_active = False
        self._trv_issue_paused = False
        ir.async_delete_issue(self.hass, DOMAIN, f"trv_unavailable_{self.entry_id}")
        ir.async_delete_issue(
            self.hass, DOMAIN, f"trv_unavailable_paused_{self.entry_id}"
        )

    async def async_request_refresh(self) -> None:
        """Coalesce concurrent refresh requests.

        If a refresh is already in progress, set a pending flag and return immediately.
        After the current refresh completes, run exactly one more pass to capture any
        changes made during the in-flight update. Prevents N×TRV-call cascades when
        multiple switches/services toggle simultaneously.
        """
        if self._refresh_in_progress:
            self._refresh_pending = True
            return
        self._refresh_in_progress = True
        try:
            while True:
                self._refresh_pending = False
                await self.async_update()
                if not self._refresh_pending:
                    break
        finally:
            self._refresh_in_progress = False

    def _validate_option(self, key: str, value: Any, duration_mins: int | None = None) -> bool:
        """Runtime validation for config options set via service calls."""
        if key == CONF_LEARNED_CLIMATE_RATE:
            return isinstance(value, (int, float)) and 0 < value <= 10.0
        if key in (CONF_PARTY_MODE_SWITCH, CONF_GUEST_MODE_SWITCH):
            if not isinstance(value, bool):
                return False
            if duration_mins is not None and (not isinstance(duration_mins, (int, float)) or duration_mins <= 0):
                return False
            return True
        return True

    async def _apply_v1_hardware(
        self, *, name: str, changes: list[dict], target_temp: float | None,
    ) -> bool:
        """V1's hardware application (extracted verbatim, gated on
        the flag by the caller). Runs ONLY pre-flip; post-flip V2 drives via the
        dispatcher and this is skipped entirely.

        Window scene management, the throttled TRV changes (with V1's per-TRV
        circuit breaker + shadow-ledger recording), the calibration applier and
        the valve positioner. Returns ``False`` iff a window opened right before
        calibration (V1's window guard) — the caller then short-circuits the
        cycle, matching V1's former inline ``return True``; ``True`` otherwise.
        """
        # ── 2. Window scene management ───────────────────────────────────
        window_open = self.engine.is_window_open()
        legacy_window = self.config.window_legacy_restore

        async def _scene_service_caller(domain, service, service_data):
            return await safe_service_call(self.hass, name, domain, service, service_data)

        is_party, _ = self.engine.check_party_mode()

        # Party ended while window still open → scene was saved with party temps,
        # discard it so closing the window won't restore stale party temperatures.
        if self._prev_party and not is_party and self._scene_manager.has_scene("window"):
            self._scene_manager.clear("window")
            self.debug_log("Party ended – window scene discarded")
        self._prev_party = is_party

        if window_open and legacy_window and not self._scene_manager.has_scene("window"):
            await self._scene_manager.save("window")
        elif not window_open and self._scene_manager.has_scene("window"):
            await self._scene_manager.restore("window", _scene_service_caller)

        # ── 4. Apply changes (Throttled Parallel - v1.5.1) ───────────────
        secs = self.config.action_delay.total_seconds()

        # 1 concurrent write per 3 TRVs — limits FRITZ!DECT radio contention
        semaphore = asyncio.Semaphore(max(len(self.config.trvs) // 3 + 1, 1))

        async def _apply_with_semaphore(change: dict[str, Any]) -> None:
            async with semaphore:
                eid = change["entity_id"]
                cb = self._cb_state.setdefault(eid, {"failures": 0, "retry_after": None})

                # Circuit breaker: skip if in backoff period
                if cb["retry_after"] and datetime.now(UTC) < cb["retry_after"]:
                    self.debug_log(f"Circuit breaker: skipping {eid} (retry after {cb['retry_after'].strftime('%H:%M')})")
                    return

                try:
                    # Track sent command for Echo-Detection
                    self.track_sent_command(eid, change.get("temperature"), change.get("hvac_mode"))
                    # V2 shadow ledger: record V1's actual send so confirm()
                    # and the override detection see the real last-commanded state.
                    if PARALLEL_VALIDATION:
                        self._record_v2_command(
                            eid, change.get("temperature"), change.get("hvac_mode"))
                    await async_apply_trv_change(self.hass, self.config.name, change, secs)
                    # Success → reset circuit breaker. Only persist when the
                    # state actually changed — otherwise every successful TRV
                    # call would write the store to disk (SD-card wear).
                    if cb["failures"] > 0:
                        _LOGGER.info("%s: %s recovered — circuit breaker reset", self.config.name, eid)
                        cb["failures"] = 0
                        cb["retry_after"] = None
                        await self._cb_save()
                except Exception as exc:
                    cb["failures"] += 1
                    # Backoff: 15 → 30 → 60 → 120min (cap at 120)
                    backoff_min = min(15 * (2 ** (cb["failures"] - 1)), 120)
                    cb["retry_after"] = datetime.now(UTC) + timedelta(minutes=backoff_min)
                    _LOGGER.warning(
                        "%s: %s failed (%d) — pausing for %dmin: %s",
                        self.config.name, eid, cb["failures"], backoff_min, exc,
                    )
                    await self._cb_save()

        if changes:
            tasks = [
                self.hass.async_create_task(_apply_with_semaphore(change))
                for change in changes
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, BaseException):
                    _LOGGER.error(
                        "%s: TRV change failed for %s: %s",
                        self.config.name,
                        changes[i].get("entity_id", "unknown"),
                        result,
                    )

        # ── 5. Calibration ───────────────────────────────────────────────
        aggressive_calib_active = self.config.aggressive_mode_selector == AGGRESSIVE_MODE_CALIBRATION

        if self.config.calibration_mode != CALIBRATION_MODE_OFF or aggressive_calib_active:
            # Verify window state again right before calibration
            if self.engine.is_window_open():
                if self.config.log_level == "debug":
                    _LOGGER.debug("%s: Calibration skipped - Window opened during update cycle", name)
                return False
            new_cals = await self._calib_applier.apply()
            self.last_calibrations.update(new_cals)

        # ── 6. Valve positioning ─────────────────────────────────────────
        if self.config.valve_mode != "off":
            await self._valve_positioner.apply(target_temp)

        return True

    def _arm_reeval_timer(self) -> None:
        """Re-arm the duration-based wakeup when there is one to arm.

        Covers the delays V1 owns for BOTH engines: persons entering/leaving,
        presence reaction, proximity confirmation, window reaction open/close and
        smart-preconditioning start (``get_next_duration_event``).

        **Why this is called before V1's early returns.**
        The arming used to sit at the very end of ``_do_update``, behind V1's
        uncertainty return and behind ``_apply_v1_hardware``'s window guard.
        Post-flip that is the wrong place: V2 keeps driving while V1 is uncertain,
        and V2's own ``next_evaluation_at`` only knows four
        boundaries (override TTL, liming end, party end, schedule transition) —
        none of the five above. So exactly when V1 went uncertain, the driving
        engine silently fell back to the 1-minute heartbeat and every duration
        delay resolved up to 60 s late.

        The pause case is deliberately still skipped by the caller.

        **Geteilter Timer-Slot**: `_reeval_timer` traegt auch
        den 30-s-Uncertainty-Retry (`async_update`). Auf einem Zyklus mit
        `controlled == False` ueberschreibt der Retry den hier gesetzten
        Duration-Timer. Kein Bug — 30 s ist frueher als jede Duration-Grenze, und
        der naechste Zyklus armt neu. Aber die Zusage „die fuenf Delays feuern
        puenktlich" gilt streng nur, solange der Raum geregelt ist; post-Flip mit
        gesundem V2 ist das der Normalfall.

        **One accepted side effect of the move**:
        ``_rate_learner.update()`` now runs AFTER this, and it mutates
        ``config.learned_climate_rate`` in place. On the single cycle where a
        learning session ends, the preconditioning boundary is therefore
        computed from the PREVIOUS rate. The EMA moves slowly and the next
        cycle (heartbeat, ≤60 s) recomputes it, so the drift is bounded and
        self-healing. Ordering it the other way is not possible: the learner
        needs a non-None target and thus sits behind the very early-return this
        method has to jump in front of.
        """
        next_event = self.engine.get_next_duration_event()
        delay = (
            (next_event - datetime.now(UTC)).total_seconds() if next_event else 0
        )
        if delay <= 0:
            # Cancel ONLY when we have a replacement.
            # The old code never reached the cancel on an uncertain cycle, so a
            # pending wakeup survived. Cancelling unconditionally here would
            # DROP it — and `get_next_duration_event()` returns None for
            # exactly the sensor that just went unavailable, i.e. precisely
            # during uncertainty. A stale timer firing once is a cheap,
            # idempotent extra update; a lost wakeup delays a real decision.
            return

        if self._reeval_timer:
            self._reeval_timer()
            self._reeval_timer = None

        self.debug_log(f"Scheduling re-evaluation in {delay + 1:.1f}s")

        @callback
        def _schedule_transition_callback(_now):
            """Triggers an update after a duration timer (e.g. window/presence) expires.
            NOTE: We intentionally do NOT set _schedule_transition_pending = True here,
            because duration timers should NOT clear manual overrides."""
            self._delayed_update(_now)

        self._reeval_timer = async_call_later(
            self.hass, delay + 1, _schedule_transition_callback
        )

    def _refresh_paused_snapshot(self) -> None:
        """In der Pause nur den Snapshot neu bauen — keine Entscheidung, kein Senden.

        `_should_run_v2` ist in der Pause aus, also lief auch der Snapshot-Bau
        nicht mehr, und alles, was `_v2_last_snapshot` liest, fror fuer die
        ganze Pause ein: Raumtemperatur (Karte + Sensor), „Personen zuhause",
        Fenster, Abwesenheit, Saison, Status-Attribute. Nach einem Neustart in
        der Pause gab es gar keinen Snapshot, die Anzeige fiel auf den
        Restore-Wert. Gemessen live 2026-11-10: Raum 16,5-17,0 °C, Tempix 18,0
        °C ueber 12 h; Person zuhause, Sensor „aus".

        Kein Trigger-Event und kein Ledger: die Override-Erkennung bleibt in
        der Pause aus (`_evaluate_trv_event` verwirft TRV-Events dort ohnehin),
        `_v2_pending_trigger` bleibt unangetastet wie bisher. Ein Fehler darf
        den Zyklus nicht brechen — die Anzeige behaelt dann den letzten Stand.

        Was der Bau nebenbei fortschreibt, wie im Normalzyklus: das
        Fenster-Gedaechtnis (`_v2_prev_window_valid`), die V2-Schliess-Marken
        (`_v2_window_close_marks`, nur bei echtem offen→zu), ueber
        `engine.is_window_open()` auch V1s `_window_close_marks` und den
        Anwesenheits-Cache von `is_anybody_home()`. V1s eigene Kette erreicht
        `is_window_open` in der Pause nicht immer (Kalender-`hvac`-Tag,
        Automatik aus) — dann schreibt erst dieser Bau V1s Marke. Folge: ein in
        der Pause geschlossenes Fenster loest nach der Pause den Close-Delay
        aus (Kalibrierung/Ventil warten, `_drive_v2_if_active`), wie im
        Dauerbetrieb; vorher fehlte er in diesem Randfall. Ein ungueltiger Kalender-`hvac:`-Tag warnt jetzt
        auch in der Pause, aber nur einmal pro Wert, weil `_warned_hvac_tags`
        am Coordinator liegt (AU-6).
        """
        try:
            ParallelValidator(self).publish_snapshot()
        except Exception as err:  # noqa: BLE001 — nur Anzeige, Zyklus muss weiterlaufen
            log = _LOGGER.debug if self._pause_snapshot_warned else _LOGGER.warning
            self._pause_snapshot_warned = True
            log(
                "TPX [%s]: snapshot refresh during pause failed, displays keep "
                "their last value: %s",
                self.config.name, err, exc_info=True,
            )

    async def _do_update(self) -> bool:
        name = self.config.name
        log = self.config.log_level
        # Default to "controlled" — a cycle where the V2 block does
        # not run (paused) or throws (the crash-loop issue owns that) must not
        # trip the paused issue. The V2 block sets it from the result validity.
        self._v2_controlled = True
        self._v2_block_reached = False
        # Was V2 in DIESEM Zyklus GEPLANT hat (post-Flip die Quelle fuer
        # `last_changes` — die Anzeige "worauf steht das Ventil", siehe die
        # Begruendung an der Zuweisung). Pro Zyklus zuruecksetzen wie
        # `_v2_controlled`: ein Zyklus ohne Plan muss leer anzeigen und nicht
        # die Kommandos des letzten.
        self._v2_sent_changes: list[dict[str, Any]] = []
        # Die Dispatch-BILANZ desselben Zyklus (nur Diagnose): pro Geraet, was
        # der Dispatcher daraus gemacht hat — inklusive der Unterdrueckungen,
        # die im Plan oben nicht sichtbar sind.
        self._last_dispatch: list[dict[str, Any]] = []

        # ── 0. Create State Snapshot ────────────────────────────────────
        snapshot = {}
        for eid in self._get_snapshot_entities():
            state = self.hass.states.get(eid)
            if state:
                snapshot[eid] = state
        self.engine.set_state_snapshot(snapshot)
        self.engine.set_startup_time(self._ready_time)

        # ── 1. Engine calculations ───────────────────────────────────────
        adj = self.engine.get_active_adjustment()
        entry_mode = self.engine.get_adjustment_mode(adj)

        set_comfort = self.engine.should_set_comfort(entry_mode)

        active = self.engine.is_automation_active()

        override_temp = None
        override_hvac = None
        if self._temporary_manual_override:
            override_temp = self._temporary_manual_override.target_temperature
            override_hvac = self._temporary_manual_override.hvac_mode

        hvac_mode = self.engine.calculate_hvac_mode(_set_comfort=set_comfort, _manual_override_hvac=override_hvac)
        target_temp = self.engine.calculate_target_temperature(_set_comfort=set_comfort, _manual_override_temp=override_temp)

        # ── Schedule Fingerprinting (V1 Fix for Boot & Window Timers) ────────
        # `sched_next` ist die 3., `away` die 4. Komponente des Fingerprints
        # (`_build_schedule_fingerprint()`), beide mit eigenem Guard.
        #
        # Einzige noch bewusst ausgeschlossene Groesse:
        #   cal_overrides – calendar events already have the highest priority in
        #                   calculate_hvac_mode / calculate_target_temperature,
        #                   so they override the output without needing to delete the override.
        last_fp_raw = getattr(self, "_last_schedule_fingerprint", None)
        current_fingerprint = self._build_schedule_fingerprint(last_fp_raw)
        
        # None und "" abfangen – kein False-Positive beim ersten Heartbeat nach Restore.
        # getattr-Default greift nur wenn Attribut FEHLT; None/"" werden explizit via `or` behandelt.
        last_fp = last_fp_raw or current_fingerprint

        # Vergleichswert fuer `_sched_next_diff_suppressible()`.
        # `_schedule_comfort_signal()` (Kalender/Helper, ohne force_comfort/
        # party/presence) statt `set_comfort` (=should_set_comfort()):
        # Letzteres kann von force_comfort/party/presence dominiert werden und
        # wuerde dann einen echten Kalender-Redirect-Wechsel maskieren.
        # Unconditional berechnet, weil die letzte
        # Zeile dieses Blocks ihn in JEDEM Fall als Baseline fuer den
        # naechsten Zyklus speichert, nicht nur im Diff-Zweig.
        current_calendar_comfort = self._schedule_comfort_signal()

        persist_suppressed_fingerprint = False
        if last_fp != current_fingerprint:
            # Bevor eine Differenz den Override loescht, pruefen, ob sie
            # AUSSCHLIESSLICH auf einer reinen Config-Aenderung von
            # `sched_next` beruht (kein echter Zeitfortschritt, keine
            # geaenderte Kalender-Comfort-Entscheidung) — siehe
            # `_sched_next_diff_suppressible()` fuer die drei Bedingungen.
            if self._sched_next_diff_suppressible(last_fp, current_fingerprint, current_calendar_comfort):
                if self._temporary_manual_override:
                    persist_suppressed_fingerprint = True
                    _LOGGER.info(
                        "TPX [%s]: sched_next-Diff unterdrueckt (%s -> %s) -- Kalender-"
                        "Comfort-Entscheidung unveraendert (%s) und die alte Grenze liegt "
                        "noch in der Zukunft. Vermutlich reine Config-Aenderung "
                        "(vacation_home/*_use_day), kein echter Phasenwechsel. "
                        "Override bleibt aktiv.",
                        self.config.name, last_fp, current_fingerprint, current_calendar_comfort,
                    )
                else:
                    self.debug_log(
                        f"sched_next-Diff unterdrueckt (kein aktiver Override): "
                        f"{last_fp} -> {current_fingerprint}"
                    )
            else:
                self._schedule_transition_pending = True
                self.debug_log(f"Schedule fingerprint changed: {last_fp} -> {current_fingerprint}")
                self._report_suspicious_fingerprint_change(last_fp, current_fingerprint)

        self._last_schedule_fingerprint = current_fingerprint
        self._last_comfort_decision = current_calendar_comfort
        if persist_suppressed_fingerprint:
            self._persist_suppressed_fingerprint()

        if self._temporary_manual_override:
            # Check 24h expiration (V1 Bugfix)
            now = datetime.now(UTC)
            expires_at = self._temporary_manual_override.timestamp + timedelta(hours=24)
            if now >= expires_at:
                self.debug_log("Temporary manual override expired (24h limit reached). Clearing.")
                self._schedule_transition_pending = True

        # ── Schedule-Transition: Override aufheben ──────────────────────────
        if self._schedule_transition_pending:
            self._schedule_transition_pending = False
            if self._temporary_manual_override:
                # SICHTBAR, nicht `debug`. Ein verschwundener
                # Nutzergriff war bisher nur im Debug-Log dokumentiert — der
                # einzige bekannte Vorfall ist auch nur deshalb
                # belegt, weil zufaellig Debug-Logging lief.
                #
                # Und es gibt keinen zweiten Kanal: Der Paritaetsvergleich
                # kann diese Klasse NICHT sehen. Der Clear passiert hier, der
                # `DecisionDiff` entsteht erst weiter unten — beide Engines
                # rechnen danach auf dem bereits geloeschten Override, der Diff
                # bleibt also null. "0 Diffs" ist fuer diesen Fehler blind.
                ov = self._temporary_manual_override
                alter_min = (datetime.now(UTC) - ov.timestamp).total_seconds() / 60
                _LOGGER.info(
                    "TPX [%s]: Temporaere Uebersteuerung (%s %s, seit %.0f min) wird "
                    "geloescht — der Zeitplan-Fingerprint hat gewechselt: %s -> %s. "
                    "War das kein echter Phasenwechsel, ist genau das der Fehler, "
                    "den wir suchen.",
                    self.config.name, ov.hvac_mode, ov.target_temperature,
                    alter_min, last_fp, current_fingerprint,
                )
                self._increment_schedule_revision()
                
                # If we cleared it, recalculate immediately so we don't apply the override on this tick
                hvac_mode = self.engine.calculate_hvac_mode(_set_comfort=set_comfort, _manual_override_hvac=None)
                target_temp = self.engine.calculate_target_temperature(_set_comfort=set_comfort, _manual_override_temp=None)

        changes, gen_offsets = self.engine.calculate_changes(
            self.last_generic_offsets,
            _target_temp=target_temp,
            _hvac_mode=hvac_mode,
        )

        self.current_hvac = hvac_mode
        self.current_temperature = target_temp
        self.current_state = self.engine.determine_heating_state()
        if self._temporary_manual_override and self.current_state not in (
            ClimateState.FROST_PROTECTION,
            ClimateState.WINDOW_OPEN,
        ):
            # Frost (chain priority) and open windows actually drive the
            # output while an override exists — showing "Temporary Manual
            # Override" would misreport why the room heats/doesn't heat.
            self.current_state = ClimateState.TEMPORARY_MANUAL_OVERRIDE
            
        self.current_reason = self._build_reason(set_comfort)

        # ── V2 Parallel Validation (exception-safe) ──────────────────────
        # Pre-Flip: V1 steuert, V2 rechnet mit (dry-run). Post-Flip: V2
        # steuert, V1 beobachtet. Ein Fehler hier darf den Zyklus NIEMALS
        # brechen.
        # Gate: siehe `_should_run_v2` — post-flip läuft V2 auch bei
        # V1-Unsicherheit, damit es fahren kann; pre-flip unverändert.
        if self._should_run_v2(target_temp, hvac_mode):
            self._v2_block_reached = True
            try:
                # Consume the pending TRV trigger: the override
                # detection runs against the real event + shadow ledger.
                v2_trigger = self._v2_pending_trigger
                self._v2_pending_trigger = None
                validator = ParallelValidator(self)
                v2_result, v2_diff, v2_detection = validator.run(
                    v1_target=target_temp,
                    v1_hvac=hvac_mode,
                    v1_state_value=self.current_state.value,
                    v1_changes=changes,
                    v1_offsets_prev=self.last_generic_offsets,
                    v1_offsets_new=gen_offsets,
                    trigger_event=v2_trigger,
                    ledger=self._v2_dispatcher.ledger_view(),
                )
                self.v2_last_result = v2_result
                # Post-Flip meldet die Anzeige, was V2 entschieden hat — nicht V1s
                # Schattenrechnung (siehe `_apply_v2_status`). VOR dem
                # Senden: die Sends warten blockierend auf jedes
                # Geraet, gemessen 21-33 s bei mehreren FRITZ!DECT-Raeumen
                # (Heizperiode an). Der Status stand so lange auf dem
                # Vorzyklus. Der Statustext haengt an nichts, was der Drive setzt;
                # `last_changes`, Kalibrierung und Degraded korrigiert der
                # spaetere Listener-Aufruf.
                self._apply_v2_status(v2_result, set_comfort)
                if ACTIVE_ENGINE == "v2":
                    self._call_listeners()
                # Post-flip V2 drives — send the commands it just
                # planned over the long-lived dispatcher. Device send failures
                # are caught inside the dispatcher (FAILED + circuit breaker);
                # only an unexpected exception raises here → the except below
                # tracks it as an active-engine failure, so the
                # success-clear only runs on a driven cycle.
                await self._drive_v2_if_active(v2_result)
                # The room is controlled iff V2 drove a SAFE
                # decision — an UNSAFE result trips the hardware safeguard
                # (nothing sent). Post-flip async_update judges loss-of-control
                # on THIS, so V1's uncertainty no longer raises the paused issue.
                self._v2_controlled = v2_result.validity is not SnapshotValidity.UNSAFE
                # DEGRADED is "controlled"
                # above and therefore invisible to the paused issue — this is
                # its own tracker, gated the same way as the crash-loop tracking.
                if ACTIVE_ENGINE == "v2":
                    self._track_validity_degraded(v2_result)
                # Post-flip a successful V2 cycle means the
                # room IS controlled → clear any crash-loop issue. Gated; no-op
                # pre-flip (V1's outer success-clear owns it until the flip).
                if ACTIVE_ENGINE == "v2":
                    self._track_active_engine_success()
                self._log_v2_detection(v2_detection, v2_trigger, name)
                # Precise boundary wakeup. Gated — inert
                # until the flip flips ACTIVE_ENGINE to "v2" (leitprinzip: the
                # flip is the single point where V1 behaviour changes).
                if ACTIVE_ENGINE == "v2":
                    self._schedule_v2_wakeup(v2_result.next_evaluation_at)
                self.v2_eval_count += 1
                if v2_diff.has_diffs:
                    self.v2_diff_count += 1
                    self.v2_last_diff = v2_diff
                    self.v2_recent_diffs.append(v2_diff)
                    # Flut-Schutz: eine PERSISTENTE Abweichung (z.B. eine
                    # bewusst akzeptierte V1/V2-Abweichung) loggt einmal
                    # WARNING, Wiederholungen DEBUG.
                    # json.dumps ist teuer → nur bauen, wenn wirklich geloggt.
                    if v2_diff.signature != self._v2_logged_signature:
                        self._v2_logged_signature = v2_diff.signature
                        _LOGGER.warning(
                            "Tempix V2 DecisionDiff (%s): %s",
                            name, json.dumps(v2_diff.to_log_dict(), default=str),
                        )
                    elif _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug(
                            "Tempix V2 DecisionDiff (%s, wiederholt): %s",
                            name, json.dumps(v2_diff.to_log_dict(), default=str),
                        )
                else:
                    # Parität stellt den Flut-Schutz scharf für die nächste
                    # NEUE Abweichung.
                    self._v2_logged_signature = None
            except Exception as err:  # noqa: BLE001 — V1 muss weiterlaufen
                # WICHTIG (Rollout-Gate): ein jeden Zyklus scheiternder V2
                # darf NICHT wie perfekte Parität aussehen. Erster Fehler
                # einer neuen Signatur → WARNING (sonst hält der Operator
                # "0 Diffs" fälschlich für grün). Wiederholungen → DEBUG.
                self.v2_error_count += 1
                error_sig = (type(err).__name__, str(err))
                if error_sig != self._v2_error_signature:
                    self._v2_error_signature = error_sig
                    _LOGGER.warning(
                        "Tempix V2 parallel validation failed (%s): %s — "
                        "V1 läuft normal weiter, aber die V2-Validierung liefert "
                        "kein Signal, bis das behoben ist.",
                        name, err, exc_info=True,
                    )
                else:
                    _LOGGER.debug(
                        "V2 parallel validation still failing (%s): %s", name, err,
                    )
                # Post-flip a V2 failure means the room is
                # UNCONTROLLED (V2 drives) → feed the SAME crash-loop repair
                # mechanism (15-min threshold, `update_failing` issue). Gated;
                # pre-flip a V2 error stays a mere WARNING (observer broken).
                if ACTIVE_ENGINE == "v2":
                    self._track_active_engine_failure(err)
        elif V2_ACTIVE and self.config.manual_mode:
            self._refresh_paused_snapshot()

        # Post-Flip muss hier stehen, was WIRKLICH rausging: `changes` ist dann
        # V1s Schattenberechnung (Beobachter), nicht der gesendete Befehl.
        # Sensor, Climate-Karte und Diagnose haetten sonst im 7-Tage-Fenster
        # ueber die abgesetzten Kommandos berichtet, die es nie gab — genau die
        # Anzeige, der man dort vertrauen koennen muss.
        self.last_changes = changes if ACTIVE_ENGINE == "v1" else self._v2_sent_changes
        self.last_generic_offsets = (
            gen_offsets if ACTIVE_ENGINE == "v1" else self._v2_dispatcher._last_generic_offsets
        )

        if log == "debug" or self.config.debug_mode:
            # target_temp can still be None here (uncertainty) — no :.1f on None
            target_str = f"{target_temp:.1f}" if target_temp is not None else "None"
            self.debug_log(
                f"active={active} hvac={hvac_mode} target={target_str} changes={len(changes)} reason={self.current_reason}"
            )

        # ── Dynamic Re-evaluation ────────────────────────────────────────
        # Armed BEFORE V1's early returns, because post-flip V2 keeps
        # driving through V1's uncertainty and needs these wakeups. Skipped in
        # pause exactly as before — there Tempix is hands-off and the old code
        # never reached the arming either.
        if not self.config.manual_mode:
            self._arm_reeval_timer()

        # Early return if uncertain
        if target_temp is None or hvac_mode is None:
            # Post-Flip fuhr V2 durch V1s Unsicherheit hindurch weiter
            # und `_apply_v2_status` hat seine Entscheidung bereits
            # veroeffentlicht — die Entities muessen davon erfahren. Sonst haelt
            # die Karte den Stand des letzten Zyklus, in dem V1 zufaellig sicher
            # war, und das ist genau die Lage, in der man ihr trauen muss.
            if ACTIVE_ENGINE == "v2":
                self._call_listeners()
            return False

        # 🟢🔴 Manual Mode / Pause (hands-off) 🟢🔴
        if self.config.manual_mode:
            # System layer: reconcile state
            # Lösche eventuell existierende temporäre Overrides, damit diese 
            # nach Beendigung der Pause nicht als "Shadow State" zurückkehren.
            # In-Cycle: kein zweiter Zyklus, und im Pause-Modus wird ohnehin
            # nichts gesendet.
            self.clear_temporary_manual_override(trigger_update=False)
            self._call_listeners()
            return True

        # ── 1.2 Climate Rate Learning (v1.5.0) ───────────────────────────
        if self.config.smart_preconditioning:
            await self._rate_learner.update(target_temp, hvac_mode)

        # ── 1.1 Notify UI early ──────────────────────────────────────────
        self._call_listeners()

        # ── V1 hardware application (V1 drives ONLY pre-flip) ─────────────
        # Post-flip V2 drove the hardware via the dispatcher (in the V2 block
        # above) and V1 is a pure OBSERVER — its apply loop (window scene, TRV
        # changes + the V1 circuit breaker, calibration + valve appliers) is
        # gated off. `_apply_v1_hardware` returns False iff a window opened right
        # before calibration (V1's window guard), short-circuiting the rest of the
        # cycle exactly as the old inline `return True` did.
        if ACTIVE_ENGINE == "v1":
            if not await self._apply_v1_hardware(
                name=name, changes=changes, target_temp=target_temp,
            ):
                return True


        # Dieselbe Umstellung wie bei `last_changes` (oben) und `_apply_v2_status`:
        # `hvac_mode`/`target_temp`/`changes` sind post-Flip V1s Schattenrechnung.
        # Diese Stelle wurde bei der ersten Umstellung uebersehen — gefunden
        # in einem Diagnose-Export, der `heat 8.0, changes_count: 1`
        # meldete, waehrend V2 `off`/`null` entschied und `planned_commands` leer
        # war. Ein Support-Fall sucht dort dann einen Dispatch, den es nie gab.
        # `current_*` und `last_changes` tragen bereits die fahrende Engine
        # (`current_*`: V1 setzt sie oben, `_apply_v2_status` ueberschreibt sie
        # post-Flip; `last_changes` waehlt die Engine direkt bei der Zuweisung).
        self._last_update = {
            "timestamp": datetime.now(UTC).isoformat(),
            "hvac_mode": self.current_hvac,
            "target_temp": self.current_temperature,
            "reason": self.current_reason,
            "changes_count": len(self.last_changes),
        }

        return True

    def _is_cooling(self) -> bool:
        """1:1 port of `engine_base.TempixEngine.is_cooling` — a static config
        property (True iff either hvac mode is exactly "cool"), no live state,
        no Snapshot needed. Verified before the swap.

        Known limitation, unchanged by this swap (deferred):
        `heat_cool`/`auto` are bidirectional HVAC modes
        that this check always classifies as "heating" — the same bug
        `engine.is_cooling` already had, moved verbatim, not fixed here. If
        this is ever resolved by making `is_cooling` dynamic/Snapshot-derived,
        this copy needs updating alongside every other `engine.is_cooling`
        reader (`v2_bridge.py`, `parallel_validation.py`,
        `coordinator_learning.py`).
        """
        return self.config.hvac_mode_eco == "cool" or self.config.hvac_mode_comfort == "cool"

    def _build_reason(self, set_comfort: bool | None = None) -> str:
        """Build a human-readable reason string from the current ClimateState.

        Args:
            set_comfort: Pre-computed comfort decision from _do_update().
                         Passed through for AWAY / weather details that need it.
                         Avoids re-calling should_set_comfort() which has
                         side-effects on _smart_preconditioning_active (CQS fix).
        """
        match self.current_state:
            case ClimateState.MANUAL_MODE:
                return "Manual Mode"

            case ClimateState.TEMPORARY_MANUAL_OVERRIDE:
                return "Temporary Manual Override"

            case ClimateState.PAUSED:
                reasons = self.engine.get_uncertainty_reasons()
                if reasons:
                    short_reasons = [r.split(".")[-1] for r in reasons[:2] if isinstance(r, str)]
                    suffix = f" ({', '.join(short_reasons)})" if short_reasons else ""
                    return f"Paused{suffix}"
                return "Paused"

            case ClimateState.INACTIVE:
                # INACTIVE hat GENAU drei Ursachen (is_automation_active,
                # engine_protection.py) — der Status nennt jede beim Namen, wie
                # die Geschwister-Zweige auch ("Frost Protection", "Window
                # Open"). Frueher stand hier nur "Off" fuer die Saison (nennt
                # die Wirkung, nicht die Ursache) und der Master-Schalter fiel
                # stumm auf "Inactive" durch — der haeufigste Abschaltgrund war
                # damit der einzige unerklaerte.
                # `season_mode` liest den Snapshot statt live `engine.is_season_mode()`
                # — reine Anzeige, verifiziertes 1:1-Feld.
                snapshot = self._v2_last_snapshot
                season_mode = bool(snapshot.season_mode) if snapshot else True
                parts = []
                if not self.config.automation_active:
                    parts.append("Automation Off")
                if not season_mode:
                    parts.append("Season Off")
                t = self.engine.check_outside_threshold()
                if t is False and season_mode:
                    parts.append("Outside < Threshold" if self._is_cooling() else "Outside > Threshold")
                return " | ".join(parts) if parts else "Inactive"

            case ClimateState.FROST_PROTECTION:
                return "Frost Protection"

            case ClimateState.WINDOW_OPEN:
                return "Window Open"

            case ClimateState.LIMING:
                return "Liming Protection"

            case ClimateState.VACATION:
                # Liest den Snapshot statt live engine.is_vacation_mode() — reine
                # Anzeige, `vacation_temperature` ist bereits ein verifiziertes
                # 1:1-Snapshot-Feld.
                snapshot = self._v2_last_snapshot
                vt = snapshot.vacation_temperature if snapshot else None
                temp = vt if vt is not None else DEFAULT_VACATION_TEMP
                return f"Vacation Mode ({temp}°C)"

            case ClimateState.PARTY:
                snapshot = self._v2_last_snapshot
                pt = snapshot.party_temperature if snapshot else None
                return f"Party Mode ({pt}°)" if pt else "Party Mode"

            case ClimateState.FORCE_COMFORT:
                return "Force Comfort"

            case ClimateState.FORCE_ECO:
                return "Force Eco"

            case ClimateState.ADJUSTMENT:
                snapshot = self._v2_last_snapshot
                mode = snapshot.adjustment_mode if snapshot else "auto"
                return f"Adjustment ({mode})"

            case ClimateState.SMART_PRECONDITIONING:
                return "Smart Pre-Conditioning"

            case ClimateState.AWAY:
                if set_comfort:
                    # Liest `calendar_tags`/`sunshine_active` aus dem Snapshot
                    # statt live engine.*() — reine Anzeige, beide bereits
                    # verifizierte 1:1-Felder. Ebenso `sunshine_offset` (die
                    # Zahl, vorher noch live engine.get_sunshine_offset()).
                    snapshot = self._v2_last_snapshot
                    parts = []
                    behavior = self.config.away_behavior
                    if behavior == "eco":
                        parts.append("🚶 Eco")
                    elif behavior == "off":
                        parts.append("🚶 Off")
                    else:
                        offset = self.config.away_offset
                        if offset != 0:
                            sign = "+" if self._is_cooling() else "-"
                            parts.append(f"🚶 {sign}{offset}°C")

                    cal_tags = snapshot.calendar_tags if snapshot else {}
                    if "comfort" in cal_tags:
                        parts.append(f"📅 {cal_tags['comfort']}°C")
                    if bool(snapshot and snapshot.sunshine_active):
                        w_offset = snapshot.sunshine_offset
                        if w_offset > 0:
                            # Sunshine REDUCES the target in BOTH modes
                            # (solar-gain compensation) — the old
                            # "+" for cooling here was a leftover of the buggy
                            # direction and misreported a correct -1.0 as +1.0.
                            parts.append(f"☀️ -{w_offset}°C")
                    suffix = f" ({', '.join(parts)})" if parts else ""
                    # Status nennt den Modus beim Namen (wie Party/Vacation);
                    # die Klammer zeigt, was Away konkret bewirkt.
                    return f"Away{suffix}"
                return "Eco"

            case ClimateState.COMFORT:
                snapshot = self._v2_last_snapshot
                parts = []
                if bool(snapshot and snapshot.holiday_today):
                    parts.append("Holiday")
                cal_tags = snapshot.calendar_tags if snapshot else {}
                if "comfort" in cal_tags:
                    parts.append(f"📅 {cal_tags['comfort']}°C")
                if bool(snapshot and snapshot.sunshine_active):
                    w_offset = snapshot.sunshine_offset
                    if w_offset > 0:
                        # Sunshine reduces the target in BOTH modes.
                        parts.append(f"☀️ -{w_offset}°C")
                suffix = f" ({', '.join(parts)})" if parts else ""
                return f"Comfort{suffix}"

            case ClimateState.ECO:
                snapshot = self._v2_last_snapshot
                parts = []
                if bool(snapshot and snapshot.holiday_today):
                    parts.append("Holiday")
                cal_tags = snapshot.calendar_tags if snapshot else {}
                if "eco" in cal_tags:
                    parts.append(f"📅 {cal_tags['eco']}°C")
                # UNERREICHBAR im V1-Pfad (gemessen, 594 Kombinationen): ist die
                # Schwelle gerissen, liefert `check_outside_threshold()` False →
                # `is_automation_active()` False → `determine_heating_state()`
                # gibt INACTIVE zurueck, bevor ECO ueberhaupt in Frage kommt.
                # Entsprechend gibt es keinen Test darauf und der String
                # "Eco (Outside > Threshold)" ist in der Praxis nie erschienen
                # (die Handbuecher behaupteten es).
                # Post-Flip kommt `current_state` aus V2 (`_apply_v2_status`);
                # ob der Zweig dort ebenso unerreichbar ist, ist nicht gemessen.
                # Deshalb stehen gelassen statt geloescht.
                t = self.engine.check_outside_threshold()
                # `season_mode` defaults to True when unconfigured/uncertain
                # (V1's own contract) — NOT `bool(snapshot and ...)`, which
                # would wrongly collapse a missing snapshot to False here.
                season_mode = bool(snapshot.season_mode) if snapshot else True
                if t is False and season_mode:
                    parts.append("Outside < Threshold" if self._is_cooling() else "Outside > Threshold")
                suffix = f" ({', '.join(parts)})" if parts else ""
                return f"Eco{suffix}"

            case _:
                return "Unknown"

    def _get_snapshot_entities(self) -> set[str]:
        """Collect all entity IDs from config that should be snapshotted."""
        entities = set()
        config = self.config

        def add(val: Any) -> None:
            """Add entity IDs from a str, list[str], or None value."""
            if not val:
                return
            if isinstance(val, str):
                entities.add(val)
            elif isinstance(val, list):
                for v in val:
                    if isinstance(v, str) and v:
                        entities.add(v)

        add(config.temp_sensor)
        add(config.outside_temp_sensor)
        add(config.weather_entity)
        add(config.scheduler_selector)
        add(config.scheduler_presence)
        add(config.season_mode_entity)
        add(config.proximity_entity)
        add(config.trvs)
        add(config.schedulers)
        add(config.persons)
        add(config.guest_mode)
        add(config.presence_sensor)
        add(config.window_sensors)
        add(config.calendar)
        # Beide werden von `_register_listeners` getrackt, fehlten hier aber:
        # der Feiertagskalender und die Entity-Referenzen in den
        # Adjustments. `_get_state` faellt dafuer still auf `hass.states` zurueck
        # — es bricht also nichts, aber die Zusage "beide Engines sehen
        # identische Inputs" hatte dort ein Loch.
        add(config.holiday_calendar)
        for entry in config.adjustments or []:
            if not isinstance(entry, dict):
                continue
            for key in ("comfort", "eco"):
                val = entry.get(key)
                # Comfort/Eco duerfen eine Zahl ODER eine Entity sein — nur
                # Entity-IDs gehoeren in den Snapshot.
                if _looks_like_entity_id(val):
                    entities.add(val)

        return entities
