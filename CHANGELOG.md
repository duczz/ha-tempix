# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

---

## [1.6.4] - 2026-05-26

### Fixed
- **Feiertags-Erkennung einen Tag zu lang aktiv:** `is_holiday_today()` Fallback behandelte das exklusive iCal-Enddatum als inklusiv — ein ganztägiger Feiertag mit `end: "2026-05-26"` (deckt nur den 25. ab) hielt `holiday_use_day` den gesamten Folgetag aktiv. Korrektur: strikter Vergleich (`>` statt `>=`). Gegenstück zum v1.6.3-Fix (dort Erkennung einen Tag zu *früh*).

### Changed (Internal / Code Quality)
- **Smart Preheating Label:** `is_cooling` Property statt direktem `_factor == -1` Check.
- **HACS zip_release:** `hacs.json` mit `zip_release: true` und `filename: "tempix.zip"`. Release-Workflow erstellt automatisch ZIP-Asset.

---

## [1.6.3] - 2026-05-06

### Fixed
- **Feiertags-Erkennung einen Tag zu früh:** `is_holiday_today()` Fallback nutzt jetzt lokale Zeitzone statt UTC — ganztägige Events (z.B. Maifeiertag) wurden ab Mitternacht Ortszeit = 22:00 UTC des Vortags erkannt.
- **Cooling-Status-Label:** Status-Sensor zeigt korrekt `Outside < Threshold` wenn Kühlung wegen zu niedriger Außentemperatur inaktiv ist.
- **Entities nach Neustart ausgegraut:** Alle Entity-Plattformen registrieren sich korrekt beim Coordinator (`async_add_listener`) mit `available`-Property.
- **Doppeltes Cancel beim Unload:** `_ha_started_listener` als benanntes Attribut gegen doppeltes Cancel.
- **Proximity-Selektor:** `EntitySelector(domain="proximity")` durch `DeviceSelector(integration="proximity")` ersetzt.
- **Liming-Zeitauswahl:** Config Flow nutzt `TimeSelector` statt Freitext-Feld.
- **Party Mode + Window Scene:** Edge Case behoben bei dem die Window-Scene aus dem Party-Modus nach Party-Ende fälschlich wiederhergestellt wurde.
- **Diagnostics Privacy:** Kalender-Event-Titel werden in Diagnose-Exports redaktiert — nur Start/End-Zeiten bleiben sichtbar.
- **Weather Entity Listener:** `weather_entity` als State-Change-Listener registriert — Sunshine Offset aktiviert sofort statt erst beim nächsten Heartbeat.
- **Holiday Calendar Listener:** `holiday_calendar` als State-Change-Listener registriert — Feiertags-Übergänge triggern sofortigen Re-Fetch.
- **Dynamic Adjustment Entity-IDs:** Referenzierte Entity-IDs in JSON-Adjustments werden als Listener registriert — Änderungen propagieren sofort.
- **Request Coalescing:** Parallele Refresh-Requests (z.B. 4 Switches gleichzeitig) werden zu einem Update + einem Follow-Up zusammengefasst. Halbiert TRV-Traffic bei parallelen Toggles.

### Added
- **Kalender-Temperatur im Status:** `Comfort (📅 21°C)` / `Eco (📅 18°C)` wenn ein Kalender-Event die Temperatur überschreibt.
- **Kalender-Tag-Kommentare:** Zeilen mit `#` in Kalender-Beschreibungen werden ignoriert.

### Improved
- **Circuit Breaker Persistence:** Backoff-State für fehlerhafte TRVs überlebt HA-Neustarts (HA Storage).
- **Smart Preheating Rate Persistence:** Gelernte Heizrate in eigenem Store statt `config_entry.options` — kein Reload-Trigger mehr.
- **Scene Storage Isolation:** Scene-Snapshots pro Entry (`tempix.scenes.{entry_id}`) statt globalem Key.
- **Dynamic Reload:** Engine-State wird nach Config-Reload zurückgesetzt.
- **Listener Consolidation:** Statische Event-Listener in einer `_listeners`-Liste zusammengefasst.
- **Calendar Lookup Warnings:** WARNING bei nicht auflösbaren `use_day`/`use_scheduler`/`holiday_use_day` Targets.

### Changed (Internal / Code Quality)
- **L-8 (FSM-Enum):** `HeatingState`-Enum in `const.py` (13 Zustände). `determine_heating_state()` in `engine_schedule.py`. `_build_reason()` auf `match/case` umgestellt. Neues Attribut `heating_state` am Status-Sensor.
- **K-4:** `config._raw` Mutationen aus `switch.py` entfernt.
- **K-3:** Type-Annotation-Pattern in `select.py` korrigiert.
- **Test-Suite (L1):** 285 Tests grün, 147 Failures durch Legacy-Mismatches behoben.
- **Translations (L2):** `translations/en.json` hinzugefügt, `test_feature_parity.py` überarbeitet.
- Fehlende Translation-Keys für alle generischen Sensoren in `de.json` / `en.json` ergänzt.
- Toter Translation-Key `"tempix"` aus `climate.py` entfernt.

---

## [1.6.2] - 2026-04-09

### Added
- **Feiertags-Erkennung (L-12):** Neue Config-Parameter `holiday_calendar` (z.B. `calendar.holidays_in_germany`) und `holiday_use_day` (Wochentag). Ist heute ein Feiertag aktiv, behandelt Tempix den Tag wie den konfigurierten Wochentag — in beiden Modi. Helper-Modus: Slot des Zieltags wird via `schedule.get_schedule` direkt aus dem HA Scheduler gelesen. Kalender-Modus: normale Delegierungs-Logik greift für den Zieltag.

### Changed
- **Vollständige Zeitanzeige Helper-Modus (L-14):** `Active Scheduler Time Period` zeigt jetzt `06:00 - 21:30` (Start + Ende) statt nur `Heizen bis 21:30`. Basis: `schedule.get_schedule` Service Call, gecacht per `calendar_scan_interval`. Feiertage zeigen den projizierten Slot mit Wochentag-Suffix: `06:00 - 21:30 (So.)`.
- **Sunshine Offset (L-15, umbenannt):** `weather_anticipation` → `sunshine_offset`, `weather_offset` → `sunshine_offset_value`. Vollständige Umbenennung in Const, Config-Model, Switch-/Number-Entities, Engine, Coordinator, Diagnostics und allen Translations. Der Offset greift jetzt nur noch bei `sunny` (nicht mehr bei `clear`). Status-Anzeige: `Comfort (☀️ -1.0°C)`.
- **Status-Verbesserungen:** `Outside > Threshold` wird jetzt auch im Eco-Status angezeigt — nicht nur bei Comfort. Feiertag wird vor Wetter im Status-String angezeigt. Away mit Emoji: `Away (🚶 -1.0°C)`. `Outside > Threshold` nur noch sichtbar wenn Saisonmodus aktiv.

### Fixed
- **Kalender-Delegation ohne Location (L-13):** `_get_delegated_event()` fällt jetzt auf `summary`-Filterung zurück wenn Kalender-Events kein `location`-Feld haben (z.B. Google Calendar). Per-Raum-Delegation funktioniert damit auch ohne Location-Metadaten.

---

## [1.6.0] - 2026-03-03

### Fixed
- **K-1**: `is_frost_protection()` wird jetzt vor `is_automation_active()` geprüft in `engine_temperature.py` + `engine_schedule.py`. Frost-Schutz greift nun auch im Saison Modus.
- **K-2**: Separates `weather_entity` Konfigurationsfeld für Weather Anticipation eingeführt. `outside_temp_sensor` Selector auf `sensor` beschränkt (Weather-Entities nur noch im `weather_entity` Feld).
- **M-B**: Multi-Sensor-Fenstererkennung: Ein offline/defekter Sensor blockiert nicht mehr die gesamte Erkennung. Mehrheitsentscheid: ≥ 50 % gültige Sensoren → Entscheidung aus gültigen Sensoren. Ein bestätigter "offen"-Sensor gewinnt immer.
- **M-E**: Party-Mode endet während Fenster offen: Window-Scene (mit Party-Temperaturen gespeichert) wird jetzt verworfen statt restored. `_prev_party` Tracking + `SceneManager.clear()` verhindern falsche Temperatur-Wiederherstellung.
- **O-3**: Kalibrierungs-Entity nicht gefunden: `CalibrationApplier` loggt jetzt einmalig ein `WARNING` je TRV wenn `calibration_mode != off` und keine passende Entity am Gerät gefunden wird. (`_calib_warned: set[str]` verhindert Log-Spam).
- **M-C**: Window-Scene wird beim Coordinator-Start verworfen (`SceneManager.clear("window")` in `async_setup`). Verhindert, dass nach einem HA-Neustart veraltete TRV-States wiederhergestellt werden, die während des Ausfalls manuell geändert wurden.
- **O-5**: Gästemodus-Entity `unavailable`: `is_guest_mode()` loggt einmalig ein `WARNING` je Entity (`_guest_warned: set[str]`). Warning wird automatisch zurückgesetzt wenn die Entity wieder verfügbar wird.
- **O-2**: Force Comfort und Force Eco schließen sich gegenseitig aus. Einschalten des einen schaltet das andere automatisch aus (UI-Update sofort via `async_write_ha_state`).

### Changed
- **Weather Anticipation**: Offset wird nur noch im Comfort-Modus (`set_comfort=True`) und bei Tages-Wetterzuständen (`sunny`, `clear`) angewendet. `clear-night` entfernt – kein Solargewinn nachts. Reason-Builder zeigt Weather-Offset ebenfalls nur noch im Comfort-Modus an.
- **Hysterese-Default**: Von 0.2 °C auf **0.3 °C** erhöht. Optimal für 0.5 °C-Stufen-TRVs (z.B. FRITZ!DECT Comet DECT) da nearest-rounding 0.2 → 0.0 Effekt ergeben würde.
- **O-3 Log-Level**: `calibration_mode: generic` ohne Kalibrierungs-Entity loggt jetzt `DEBUG` statt `WARNING` – bei FRITZ!DECT ist das erwartetes Verhalten. `native`-Modus ohne Entity bleibt `WARNING`.
- **Away-Offset Feldbeschreibung**: Hinweis ergänzt, dass Offset=0 keinen Temperatureffekt hat.
- **Dokumentation**: Admin- und Endanwender-Handbuch aktualisiert (Kalibrierung `generic`, FRITZ!DECT Beispiel, Force Comfort/Eco Ausschluss, Frostschutz im Saisonmodus, Fenster-Scene Neustart-Verhalten).
- **M-A revertiert**: Hysterese-Deadband aus `engine_temperature.py` entfernt. Der Sollwert wird wieder unverändert ans TRV gesendet; das `hysteresis`-Feld hat damit aktuell keinen Effekt auf die Zieltemperatur-Berechnung.

## [1.5.9] - 2026-03-02

### Changed
- **Translations bereinigt**: `translations/en.json` gelöscht – HA fällt automatisch auf `strings.json` zurück. Nur noch 2 Dateien: `strings.json` (EN) + `translations/de.json` (DE).
- **strings.json Fixes**: Hysterese-Beschreibung mit konkretem Beispiel, "Force Eco/Comfort Temperatur" → "Temperature" (Tippfehler).

## [1.1.6] - 2026-03-01

### Added
- **Outside Temperature Hysteresis**: Neuer Konfigurationsparameter `outside_temp_hysteresis` (0–5 °C, Standard 1.0 °C). Verhindert schnelles Hin- und Herschalten nahe der Außentemperatur-Schwelle mit echtem State-basiertem Hysterese-Algorithmus (`_last_outside_ok`).
- **Config Flow Bug Fix**: Optionale Entity-Felder (outside_temp_sensor, proximity_entity, etc.) können nun im Options-Flow gelöscht werden. Fix: `suggested_value` statt `default`, Tombstoning fehlender Keys als `None`.

## [1.1.5] - 2026-02-28

### Changed
- **Coordinator Refactoring (Single Responsibility)**: `coordinator.py` von ~1116 auf ~794 Zeilen reduziert durch Extraktion von 3 Helfer-Modulen:
  - `coordinator_scene.py` – `SceneManager` (Fenster-/Party-Szenen)
  - `coordinator_appliers.py` – `CalibrationApplier`, `ValvePositioner`, `safe_service_call`
  - `coordinator_learning.py` – `HeatingRateLearner` (Smart Preheating)

### Fixed
- **Async-Korrektheit**: `asyncio.gather(*tasks, return_exceptions=True)` für parallele TRV-Updates mit Error-Logging je TRV.
- **Background Tasks**: `_create_tracked_task()` Helper + `_background_tasks: set` + `async_unload` cancelt alle Tasks.
- **Calendar Fetch**: `_async_fetch_calendar_events()` in `async_update` mit `try/except` umgeben.
- **Diagnostics Constant**: `_DIAG_ATTRS` als `frozenset` Modul-Konstante statt inline `set`.

## [1.1.4] - 2026-02-28

### Changed
- **Dictionary → Dataclass Migration**: Alle ~226 `.get()` Config-Zugriffe durch typisierte `TempixConfig`-Dataclass ersetzt. Neues File `config_model.py` mit `from_dict()`/`to_dict()`. Durations als `timedelta`, Entity-Listen als `list[str]`, kein Runtime-Parsing im Heiz-Loop.
- **Engine Calendar Extraktion**: Kalender-Logik aus `engine_schedule.py` in eigenes `engine_calendar.py` extrahiert.

## [1.1.3] - 2026-02-27

### Fixed
- **Switch Live-Reaktion**: `async_turn_on` löst jetzt sofort ein Coordinator-Refresh aus (`async_request_refresh`), sodass Switches wie Weather Anticipation beim Einschalten sofort wirken statt auf den nächsten Heartbeat (60s) zu warten. `async_turn_off` hatte diesen Aufruf bereits.
- **Active Scheduler Sensor**: Der Sensor zeigte den Kalendernamen an, obwohl Scheduler-Helper-Modus aktiv war. Ursache: `_get_scheduler_name()` prüfte den Kalender immer zuerst per `force_check=True`. Fix: Kalender wird nur im Kalender-Modus als primäre Quelle angezeigt.

## [1.1.2] - 2026-02-27

### Removed
- **Legacy Blueprint-Helper entfernt**: `CONF_TEMPERATURE_COMFORT_ENTITY` und `CONF_TEMPERATURE_ECO_ENTITY` vollständig aus dem Codebase entfernt (8 Dateien). Die Integration nutzt jetzt ausschließlich interne Number-Entities (`Comfort Temperature` / `Eco Temperature`) statt externer `input_number`-Helper.
- **`calculate_reset_data()` bereinigt**: Die Logik zum Zurückschreiben an externe Helper-Entities wurde entfernt, da die Number-Entities über `config_entries` gesteuert werden.

## [1.1.1] - 2026-02-27

### Changed
- **Default-Werte verbessert**: Aggressiver Erkennungs-Bereich auf 0,3 (vorher 0,0), Aggressiver Offset auf 1,0 (vorher 0,0), Weather Anticipation Offset auf 1,0 (vorher 0,5), Party-Temperatur auf 18 °C (vorher 20 °C).

### Fixed
- **Keine Entitäten bei neuem Eintrag**: Migrationslogik prüfte nur `entry.options` (bei neuen Einträgen leer), was das Plattform-Setup verhinderte. Fix: `merged_config = {**entry.data, **entry.options}` für korrekte Prüfung.
- **Test-Setup**: Fehlenden Mock für `ConfigEntryNotReady` in `conftest.py` ergänzt.

## [1.1.0] - 2026-02-12

### Added
- **Feature Status Verification**: Comprehensive test suite for presence, scheduler, valve positioning, and adjustments.
- **Diagnostic Sensors**: Added `TRV Temp` and `TRV Target` sensors for better visibility into the engine's internal calculations.
- **Debug Mode Switch**: A dedicated switch to enable/disable detailed logging without requiring a config reload.
- **Auto-Update Selection**: Option to pause the main automation loop via a switch.

### Changed
- **Config Flow Consolidation**: Merged multiple setup steps into a single, streamlined initial configuration screen.
- **Automatic Unit Detection**: The integration now automatically detects Celsius vs. Fahrenheit.
- **Refactored Engine Logic**: Improved calibration stability and fixed the "off-above" calibration skip bug.
- **Modern Python Standards**: Switched from deprecated `utcnow()` to UTC-aware `datetime.now(UTC)`.

### Fixed
- **Calibration Drift**: Fixed issues where offsets were not correctly updated when TRVs were in "off" mode.
- **Mocking Logic**: Standardized test imports and mocks to allow reliable standalone execution.
- **NameError Fixes**: Resolved various undefined variable errors in `sensor.py` and `engine.py`.
- **Liming Protection**: Fixed float conversion errors in the liming logic.

