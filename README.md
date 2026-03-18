[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![HA Version](https://img.shields.io/badge/HA%20Minimum-2024.6.0-blue)](https://www.home-assistant.io)
[![Version](https://img.shields.io/badge/Version-1.6.0-green)](https://github.com/duczz/ha-tempix/releases)
[![IoT Class](https://img.shields.io/badge/IoT%20Class-local__push-brightgreen)](https://www.home-assistant.io/integrations)

# 🏠 Tempix

> **Intelligente, ereignisbasierte Heizungs- & Klimaanlagensteuerung für Home Assistant.**
> 
> Verwandelt deine TRV-Thermostate in ein adaptives, selbstlernendes Heizsystem – rein lokal, ohne Cloud, ohne Abo.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=duczz&repository=ha-tempix&category=integration)

## Support me ☕

Wenn dir diese Integration gefällt und du meine Arbeit unterstützen möchtest, freue ich mich über eine kleine Spende. <br>
Vielen Dank für deine Unterstützung! ❤️

<a href="https://www.paypal.com/donate/?hosted_button_id=GBCCKFTK5FVX4">
  <img src="https://github.com/duczz/ha-cryptoinfo-advanced/blob/master/paypal_donation_logo.png?raw=true" width="230" alt="Donate with PayPal">
</a>

---

## ✨ Features

- **📅 Zeitplan- & Kalendersteuerung** – Schedule-Helper oder Google/iCloud-Kalender. Profi-Befehle in der Kalender-Beschreibung (`comfort: 21.5`, `time: 08:00 - 20:00`, `use_day: sunday`)
- **👥 Anwesenheitserkennung** – Personen, Geräte, Proximity (Geo-Fencing) und Präsenz-Sensoren. Automatische Absenkung bei Abwesenheit
- **🪟 Fenster-Reaktion** – Multi-Sensor-Fenstererkennung mit Mehrheitsentscheid. Ein defekter Sensor blockiert nicht mehr die gesamte Erkennung
- **🧭 TRV-Kalibrierung** – Generisch (alle TRVs), Native Offset (Tado, ZigBee) oder komplett aus
- **❄️ Frostschutz** – Greift auch wenn der Saison-Modus aktiv ist (Sommerbetrieb)
- **🚀 Aggressiver Modus (Push-To-Target)** – Erzwingt schnelles Aufheizen nach Abwesenheit oder Lüften
- **⏱️ Smart Preheating** – Lernt Aufheizrate (EMA). Startet automatisch zum richtigen Zeitpunkt
- **☀️ Wetter-Vorausschau** – Reduziert Comfort-Temp bei Sonnenschein passiv
- **🎉 Party- & Gäste-Modus** – Mit optionalem Auto-Timer via Service
- **🏖️ Saison-Modus** – Außentemperatur-Gate mit Hysterese verhindert stetes Hin- und Herschalten
- **🔧 Ventilpositionierung** – Direkte Ventil-% Steuerung für unterstützte TRVs
- **🔄 Dynamic Reload** – Temperatures, Offsets & Switches sofort live ohne Neustart
- **📊 HA Diagnostik** – Vollständiger State-Snapshot als JSON-Download
- **🛡️ Vollständig lokal** – Kein Cloud-Account, keine API-Keys, keine externen Abhängigkeiten

---

## 🌡️ Unterstützte Geräte

| Hersteller / Protokoll | Kalibrierungsmodus | Anmerkung |
|---|---|---|
| AVM FRITZ!DECT Comet DECT | `generic` | Kein Kalibrierungs-Entity – Offset via Zieltemperatur |
| Tado | `native` (Tado Service) | Automatische Erkennung via Device Registry |
| Zigbee TRVs (MOES, Tuya, Sonoff, IKEA) | `native` oder `generic` | Abhängig von verfügbarer Kalibrierungs-Entity |
| Z-Wave Thermostate | `generic` | Universell kompatibel |
| Alle `climate.*` Entities | `generic` | Plattform-agnostisch |

> **Voraussetzung:** Mindestens eine `climate.*` Entity (TRV oder Thermostat). Externer Temperatursensor empfohlen.

---

## 📦 Installation

### Via HACS (empfohlen)

1. Öffne HACS → **Integrations**
2. Klicke auf `⋮` → **Benutzerdefinierte Repositories**
3. URL: `https://github.com/duczz/ha-tempix`, Kategorie: **Integration**
4. Suche nach **Tempix** und installiere
5. Home Assistant neu starten
6. **Einstellungen → Geräte & Dienste → + Integration hinzufügen** → `Tempix`

### Manuell

```bash
# In deinem HA config Verzeichnis:
mkdir -p custom_components/tempix
# Kopiere alle Dateien aus dem Repository in diesen Ordner
```

Danach HA neu starten und via UI konfigurieren.

---

## ⚡ Schnellstart

### Minimalkonfiguration

Die Konfiguration erfolgt vollständig über den **Config Flow** (6 Schritte):

1. **Name** vergeben (z.B. `Wohnzimmer`)
2. **TRV(s)** auswählen (`climate.wohnzimmer_comet_dect`)
3. **Temperatursensor** angeben (`sensor.wohnzimmer_temperatur`)
4. **Schedule** oder **Kalender** konfigurieren
5. **Personen** & Fenster-Sensoren optional hinzufügen
6. **Kalibrierung** & Schutzfunktionen einstellen

**Fertig!** Die Integration erstellt automatisch alle Entities (~21 pro Raum).

---

## 🏗️ Architektur

```
[Externe Entities (TRV, Sensoren, Kalender)]
              ↓ State Change Events
     [Coordinator – Event-Loop / Debounce]
              ↓  asyncio.Semaphore(3)
         [Engine – 7 Mixins]
              ↓
    [calculate_target_temp + hvac_mode]
              ↓  Service Calls (Retry 2×)
    [TRV → climate.set_temperature]
              ↓
    [HA State Machine → UI / Automationen]
```

| Schicht | Dateien | Aufgabe |
|---|---|---|
| **Typed Config** | `config_model.py` | Einmaliges Parsing beim Start, typisierte Dataclass |
| **Engine** | `engine.py` + 7 Mixins | Reine Logik: Temperatur, Präsenz, Schutz, Kalender, Kalibrierung |
| **Coordinator** | `coordinator.py` + 3 Helfer | State-Listener, Timer, TRV-Ansteuerung |
| **Entities** | `climate/sensor/switch/number/select` | 21+ Entities pro Instanz |

---

## 🤖 Beispiel-Automation

```yaml
# Party-Modus beim Feierabend für 4 Stunden
automation:
  - alias: "Freitags-Party automatisch starten"
    trigger:
      - platform: time
        at: "17:30:00"
    condition:
      - condition: time
        weekday: [fri]
    action:
      - service: tempix.set_party_mode
        data:
          status: true
          duration: 240  # Minuten
```

```yaml
# Template: Heizgrund im Dashboard anzeigen
template:
  - sensor:
      - name: "Wohnzimmer Heizgrund"
        state: "{{ state_attr('climate.wohnzimmer_heizung', 'reason') }}"
```

---

## 📷 Dashboard

```yaml
type: vertical-stack
cards:
  - type: thermostat
    entity: climate.wohnzimmer_heizung
  - type: entities
    entities:
      - sensor.wohnzimmer_heizung_status
      - binary_sensor.wohnzimmer_heizung_window_open
      - binary_sensor.wohnzimmer_heizung_anybody_home
      - switch.wohnzimmer_heizung_party_mode_switch
      - switch.wohnzimmer_heizung_force_comfort_temperatur
      - switch.wohnzimmer_heizung_force_eco_temperatur
```

---

## 📚 Dokumentation

| Dokument | Zielgruppe |
|---|---|
| [Administrator-Handbuch](.docs/admin_handbuch.md) | Technisch: Architektur, Alle Parameter, Debugging |
| [Endanwender-Handbuch](.docs/endanwender_handbuch.md) | Praktisch: Jede Funktion mit Beispiel & Schritt-für-Schritt |
| [Changelog](CHANGELOG.md) | Alle Versionen mit Details |
| [Release Notes v1.6.0](release_notes_v160.md) | Was ist neu in v1.6.0 |

---

## 🔐 Datenschutz

- **Rein lokal** – Kein Datentransfer, keine Cloud
- Keine API-Keys, keine externen Abhängigkeiten
- Alle Berechnungen finden ausschließlich auf dem HA-Host statt
- Diagnose-Exports enthalten Entity-IDs und lokale Sensor-Werte (nur auf Anfrage herunterladbar)

---

## 🆘 Support & Issues

- **Diagnose-Daten:** Einstellungen → Geräte → Tempix → `⋮` → **Diagnose herunterladen**
