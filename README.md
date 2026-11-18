<div align="center">

<h1><img src="custom_components/tempix/brand/icon.png" width="60" height="60" valign="middle" alt="Tempix">&nbsp;&nbsp;Tempix</h1>

### Intelligente, ereignisbasierte Heizungs- & Klimaanlagensteuerung für Home Assistant.

[![HACS][hacs-badge]][hacs-url]
[![Home Assistant][ha-badge]][ha-url]
[![Release][version-badge]][release-url]
[![IoT Class][iot-badge]][iot-url]

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge&logo=homeassistantcommunitystore&logoColor=white
[hacs-url]: https://hacs.xyz
[ha-badge]: https://img.shields.io/badge/Home%20Assistant-2024.7+-41BDF5.svg?style=for-the-badge&logo=homeassistant&logoColor=white
[ha-url]: https://www.home-assistant.io
[version-badge]: https://img.shields.io/badge/dynamic/json?url=https://raw.githubusercontent.com/duczz/ha-tempix/main/custom_components/tempix/manifest.json&query=$.version&label=release&color=22c55e&style=for-the-badge&logo=github&logoColor=white
[release-url]: https://github.com/duczz/ha-tempix/releases/latest
[iot-badge]: https://img.shields.io/badge/IoT%20Class-local__push-22c55e.svg?style=for-the-badge
[iot-url]: https://www.home-assistant.io/integrations

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=duczz&repository=ha-tempix&category=integration)

</div>

---

Verwandle deine HVAC-Systeme, smarten Thermostate & Klimaanlagen in ein adaptives, selbstlernendes Heiz- und Kühlsystem. 100% lokal & ohne Cloud. Mit Zeitplänen, Anwesenheitserkennung, Fenstersensoren und intelligenter Vorabregelung.

---

## Support me ☕

Wenn dir diese Integration gefällt und du meine Arbeit unterstützen möchtest, freue ich mich über eine kleine Spende. <br>
Vielen Dank für deine Unterstützung! ❤️

<a href="https://www.paypal.com/donate/?hosted_button_id=GBCCKFTK5FVX4">
  <img src="https://github.com/duczz/ha-tempix/blob/main/.github/paypal_donation_logo.png?raw=true" width="230" alt="Donate with PayPal">
</a>
---

## ✨ Features

- **📅 Zeitplan & Kalender** – Schedule-Helper oder Kalender (z. B. Google, iCloud); Temperaturen und Zeiten auch per Befehl in der Termin-Beschreibung
- **👥 Anwesenheit** – Personen, Geräte, Proximity und Präsenz-Sensoren; bei Abwesenheit fester Eco-Wert, Offset, Abschalten oder Ignorieren
- **🪟 Fenster-Erkennung** – Mehrere Sensoren pro Raum; fällt einer aus, entscheiden die erreichbaren
- **🧭 TRV-Kalibrierung** – Generisch (alle TRVs), nativer Offset (Tado, Zigbee) oder aus
- **❄️ Frostschutz** – Untergrenze für die Zieltemperatur; heizt notfalls auch bei ausgeschalteter Automatik, außer bei offenem Fenster
- **🚀 Aggressiver Modus** – Schnelles Aufheizen nach Abwesenheit oder Lüften
- **⏱️ Smart Pre-Conditioning** – Lernt die Aufheizrate und startet rechtzeitig vor der nächsten Comfort-Phase
- **☀️ Sonnenschein-Offset** – Senkt die Comfort-Temperatur bei Sonnenschein
- **🎌 Feiertage** – Automatischer Zeitplan-Wechsel über die HA-Holiday-Integration
- **🎉 Party- & Gäste-Modus** – Per Schalter oder Service, optional mit Timer
- **✈️ Urlaub Abwesend** – Eigene Urlaubstemperatur, per Schalter oder Kalender-Termin
- **🏡 Urlaub Zuhause** – Fährt per Schalter den Feiertags-Zeitplan, ganz ohne Kalender-Eintrag
- **🍂 Saison-Modus** – Außentemperatur-Schwelle mit Hysterese, kein ständiges Hin- und Herschalten
- **🔧 Ventilsteuerung** – Direkte Ventil-%-Steuerung für TRVs mit eigener Ventil-Entity, Kennlinie wählbar
- **🔘 Comfort / Eco erzwingen** – Per Schalter sofort Comfort- oder Eco-Temperatur, unabhängig vom Zeitplan
- **✋ Temporäre manuelle Steuerung** – Drehst du am Thermostat, hält Tempix deinen Wert bis zur nächsten Zeitplan-Phase (max. 24 h); Fenster und Frostschutz gehen vor
- **⏸️ Manueller Modus (Pause)** – Tempix regelt nicht; das Tempix-Thermostat reicht deine Eingaben 1:1 an die Geräte durch
- **🚿 Entkalkungsschutz** – Wöchentliche Ventilspülung zu Wunschtag und -uhrzeit
- **🔄 Ohne Neustart** – Temperaturen, Offsets und Schalter wirken sofort
- **⚠️ Reparatur-Meldungen** – Hinweis unter *Einstellungen → System → Reparaturen*, wenn ein Raum ungeregelt bleibt oder eine Einstellung ins Leere zeigt
- **📊 Diagnose-Export** – Vollständiger Zustand als JSON-Download
- **🛡️ Vollständig lokal** – Kein Cloud-Account, keine API-Keys, keine externen Abhängigkeiten

Alle Funktionen mit Beispielen: [Endanwender-Handbuch](#-dokumentation).

---

## 🌡️ Unterstützte Geräte

| Hersteller / Protokoll | Kalibrierungsmodus | Anmerkung |
|---|---|---|
| AVM FRITZ!DECT Comet DECT | `generic` | Kein Kalibrierungs-Entity – Offset via Zieltemperatur |
| Tado | `native` (Tado Service) | Automatische Erkennung via Device Registry |
| Zigbee TRVs (MOES, Tuya, Sonoff, IKEA) | `native` oder `generic` | Abhängig von verfügbarer Kalibrierungs-Entity |
| Z-Wave Thermostate | `generic` | Universell kompatibel |
| Alle `climate.*` Entities, auch Klimaanlagen | `generic` | Plattform-agnostisch, Heizen und Kühlen |

> **Voraussetzung:** Mindestens eine `climate.*` Entity (TRV, Thermostat oder Klimaanlage). Externer Temperatursensor empfohlen.

---

## 📦 Installation

1. Tempix über den Button oben in HACS öffnen und **Herunterladen**. Ohne Button: HACS → `⋮` → **Benutzerdefinierte Repositories** → `https://github.com/duczz/ha-tempix`, Kategorie **Integration**.
   Ab Home Assistant 2026.11 ersetzt der **Marketplace** HACS: Der Button oben öffnet Tempix dort. Ohne Button: **Einstellungen → Marketplace → Aus Link hinzufügen** → `duczz/ha-tempix` → **Hinzufügen** → **Installieren** (dafür muss GitHub verbunden sein).
2. Home Assistant neu starten
3. **Einstellungen → Geräte & Dienste → + Integration hinzufügen** → `Tempix`

Manuell: den Ordner `custom_components/tempix` nach `<HA-config>/custom_components/tempix` kopieren, dann ab Schritt 2.

---

## ⚡ Schnellstart

Die Konfiguration erfolgt vollständig über den **Config Flow**: erst einen **Namen** vergeben (z. B. `Wohnzimmer`), dann sechs Schritte:

1. **Geräte & Sensoren:** TRV(s), Raumtemperatursensor, Fenster- und Außensensor
2. **Klima:** Temperaturen, Modi, Zeitplan-Anpassungen, Kalibrierung
3. **Kalender** oder **Schedule-Helper** (je nach Wahl in Schritt 2)
4. **Anwesenheit:** Personen & Präsenz (optional)
5. **Verhalten:** Saison, Abwesenheit, aggressiver Modus
6. **Erweitert:** Ventil, Frost- & Kalkschutz, Timing

> **Ein Thermostat gehört zu genau einem Raum.** Wählst du beim Einrichten ein Thermostat, das ein anderer Tempix-Raum schon steuert, zeigt der Dialog einen Fehler.

**Fertig!** Die Integration erstellt automatisch alle Entities (37 pro Raum, plus 2 je TRV).

---

## ⬆️ Update von 1.6.x auf 1.7.0

- **Mindestversion** ist jetzt Home Assistant 2024.7.0.
- **Pause-Schalter:** Der Key heißt jetzt `manual_mode` statt `manual_override` (Attribut, Status-Text, `heating_state`). Die Entity-ID bestehender Installationen bleibt. **Prüfen:** Dashboards, Templates und Automationen, die `manual_override` lesen.
- **Speicherort:** Tempix-Dateien ziehen beim ersten Start automatisch nach `.storage/tempix/` um. Die alten Dateien nicht von Hand löschen.

Alle Änderungen: [Changelog](CHANGELOG.md).

---

## 🏗️ Architektur

```
[Externe Entities (TRV, Sensoren, Kalender)]
              ↓ State Change Events
     [Coordinator – Event-Loop / Debounce]
              ↓
      [Snapshot – unveränderliche Momentaufnahme]
              ↓
   [Policies – Ziel → Modifikatoren → Grenzen]
              ↓  lückenlose Begründungskette
      [Entscheidung + Gerätekommandos]
              ↓  Dispatcher: Retry, Ausfallsicherung, Quittungen
    [TRV → climate.set_temperature]
              ↓
    [HA State Machine → UI / Automationen]
```

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
          # config_entry_id: <id>  # optional: nur EINEN Raum setzen; ohne Angabe = alle Räume (haus-weit)
```

> `set_party_mode` und `set_guest_mode` wirken ohne `config_entry_id` **haus-weit** (alle Räume gleichzeitig). Mit optionalem `config_entry_id` wird nur der gewählte Raum gesetzt — der Modus-Switch dieses Raums geht dann sichtbar an, die anderen bleiben unberührt.

Den Heizgrund zeigt `sensor.<name>_status` direkt an. Ein Beispiel-Dashboard steht im Endanwender-Handbuch, Kapitel 9.

---

## 📚 Dokumentation

| Dokument | Zielgruppe |
|---|---|
| [Endanwender-Handbuch](docs/endanwender_handbuch.md) | Praktisch: Jede Funktion mit Beispiel & Schritt-für-Schritt |
| [Changelog](CHANGELOG.md) | Alle Versionen |

---

## 🔐 Datenschutz

- **Rein lokal** – Kein Datentransfer, keine Cloud
- Keine API-Keys, keine externen Abhängigkeiten
- Alle Berechnungen finden ausschließlich auf dem HA-Host statt
- Diagnose-Exports enthalten Entity-IDs und lokale Sensor-Werte (nur auf Anfrage herunterladbar)
- Im Export werden **entfernt oder pseudonymisiert**: Personennamen, Kalender-Titel und -Orte, Kalender-Entity-IDs (häufig Mailadressen) sowie Zonennamen
- **Kalender-Zeiten bleiben enthalten:** Start- und Endzeiten (keine Titel, keine Orte) aller konfigurierten Kalender für die kommenden sieben Tage — ohne sie ist die Zeitplan-Logik nicht diagnostizierbar. Wenn du einen **privaten** Kalender eingetragen hast, sieh den Export durch, bevor du ihn an ein öffentliches Issue hängst

---

## 🆘 Support & Issues

- **Fehler & Wünsche:** [GitHub Issues](https://github.com/duczz/ha-tempix/issues)
- **Diagnose-Daten anhängen:** Einstellungen → Geräte → Tempix → `⋮` → **Diagnose herunterladen**
