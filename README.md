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

- **📅 Zeitplan- & Kalendersteuerung** – Schedule-Helper oder Google/iCloud-Kalender. Profi-Befehle in der Kalender-Beschreibung (`comfort: 21.5`, `time: 08:00 - 20:00`, `use_day: sunday`). Aktive Kalender-Temperaturen sichtbar im Status-Sensor (`Comfort (📅 21°C)`). Zeilen mit `#` werden als Kommentare ignoriert.
- **👥 Anwesenheitserkennung** – Personen, Geräte, Proximity (Geo-Fencing) und Präsenz-Sensoren. Konfigurierbares Verhalten bei Abwesenheit: Fester Eco-Wert, dynamischer Temperatur-Offset, komplett Abschalten oder Ignorieren — wirksam innerhalb der Comfort-Phasen des Zeitplans (in der Eco-Phase gilt ohnehin schon der abgesenkte Wert).
- **🪟 Fenster-Reaktion** – Mehrere Fenstersensoren pro Raum: ein offener Sensor genügt; fällt ein Sensor aus, entscheiden die erreichbaren
- **🧭 TRV-Kalibrierung** – Generisch (alle TRVs), Native Offset (Tado, ZigBee) oder komplett aus
- **❄️ Frostschutz** – Untergrenze auf die Zieltemperatur; ist die Automatik komplett aus (Saison, Master-Schalter oder toter Außensensor) und der Raum droht trotzdem zu frieren, schaltet Tempix die Heizung aktiv ein — außer bei offenem Fenster
- **🚀 Aggressiver Modus (Push-To-Target)** – Erzwingt schnelles Aufheizen nach Abwesenheit oder Lüften
- **⏱️ Smart Pre-Conditioning** – Lernt Aufheizrate (EMA). Startet automatisch zum richtigen Zeitpunkt
- **☀️ Sunshine Offset** – Reduziert Comfort-Temp bei Sonnenschein (nur `sunny`)
- **🎌 Feiertags-Erkennung** – Automatischer Zeitplan-Wechsel an Feiertagen via HA Holiday-Integration
- **🎉 Party- & Gäste-Modus** – Mit optionalem Auto-Timer via Service
- **✈️ Urlaub Abwesend** – Eigene Urlaubstemperatur (Urlaubs-Modus), per Switch oder Kalender-Event aktivierbar
- **🏖️ Urlaub Zuhause** – Schalter pro Raum, der den Wochenend-/Feiertags-Zeitplan (`holiday_use_day`) manuell aktiviert, ganz ohne Kalender-Eintrag oder echten Feiertag. Ideal für Urlaub zuhause. Optional eigener Wochentag (`vacation_home_use_day`, z. B. Samstag statt Sonntag) – echte Feiertage und Urlaub zuhause können so unterschiedliche Zeitpläne fahren.
- **🏖️ Saison-Modus** – Außentemperatur-Gate mit Hysterese verhindert stetes Hin- und Herschalten
- **🔧 Ventilpositionierung** – Direkte Ventil-%-Steuerung für TRVs mit eigener Ventil-Entity, mit vier wählbaren Kennlinien ([Details](#-ventilpositionierung))
- **🔘 Force Comfort / Force Eco** - Ad-hoc-Override per Switch: erzwingt sofort Comfort- oder Eco-Temperatur unabhängig vom Zeitplan
- **⏱️ Temporary Override (Echo-Tracking)** - Drehst du manuell am Thermostat (oder an der Tempix-Karte), friert Tempix den Zeitplan ein, bis zur nächsten Zeitplan-Phase (oder max. 24h). Schutzfunktionen wie Fenster-Erkennung und Frostschutz übersteuern den Override jederzeit. Lässt sich per HA-Switch abbrechen oder setzen.
- **🤫 Manual Mode / Pause** - Hands-off-Modus: Tempix greift komplett zurück - die Tempix-Karte funktioniert dabei als direkte Fernbedienung und reicht deine Eingaben 1:1 an die Geräte durch (Passthrough), ohne die Integration deaktivieren zu müssen
- **🔩 Liming-Schutz** – Automatische Zwangsspülung des Ventils (konfigurierbarer Wochentag & Uhrzeit) verhindert Kalkablagerungen bei langen Stillstandsphasen
- **🔄 Dynamic Reload** – Temperatures, Offsets & Switches sofort live ohne Neustart
- **📊 HA Diagnostik** – Vollständiger State-Snapshot als JSON-Download
- **⚠️ Automatische Reparatur-Meldungen** – Bleibt ein Raum ungeregelt oder zeigt eine Einstellung ins Leere (z. B. gelöschte Entity, Comfort/Eco in der falschen Richtung), erscheint ein Hinweis unter Einstellungen → System → Reparaturen. Er verschwindet, sobald der Raum wieder normal läuft bzw. die Einstellung stimmt.
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

## 🔧 Ventilpositionierung

Manche TRVs stellen ihre Ventilöffnung als eigene `number.*`-Entity bereit. Tempix kann sie
direkt setzen, statt nur eine Zieltemperatur zu senden — das Ventil öffnet dann genau so weit,
wie es die Abweichung zum Ziel rechtfertigt.

**Wer das braucht:** nur Setups mit einer echten Ventil-Entity. Wer keine hat, lässt die
Einstellung auf *Aus* — es ändert sich nichts.

Die Öffnung ergibt sich aus der Differenz zwischen Ziel- und Raumtemperatur, skaliert auf die
konfigurierte **maximale Differenz** (ab dieser Abweichung geht das Ventil voll auf):

| Kennlinie | Verhalten | Wann sinnvoll |
|---|---|---|
| **Aus** | Tempix fasst die Ventil-Entity nicht an | Standard |
| **Linear** | Öffnung steigt gleichmäßig mit der Abweichung | Der neutrale Fall |
| **Pessimistisch** | Öffnet **früher** — schon bei kleiner Abweichung deutlich weit | Träge Räume, schlecht gedämmt, Heizkörper unterdimensioniert |
| **Optimistisch** | Öffnet **später** — bleibt bei kleiner Abweichung zurückhaltend | Gut gedämmte Räume, Überschwingen soll vermieden werden |

**Mindestöffnung (Schließpunkt):** Bleibt dein Ventil bis zu einer bestimmten Öffnung physisch
zu (z. B. bis 18 %), trag diesen Wert ein. Tempix verteilt dann jede Öffnung über 0 auf den
Bereich von der Mindest- bis zur maximalen Öffnung, sodass schon die kleinste Öffnung wirkt.
Beispiel mit Mindestöffnung 18, Schrittweite 10 und linearer Kennlinie: statt 10 / 50 / 100 %
sendet Tempix 28 / 58 / 100 %. Zu (0 %) bleibt zu. Standard 0 = keine Mindestöffnung.

Drei Sonderfälle:

- **Gerät aus → Ventil zu.** Berechnet Tempix „aus" (Eco-Modus auf *Aus*, Abwesenheit,
  Ruhezustand), wird das Ventil geschlossen — auch wenn der Raum unter dem Zielwert liegt.
- **Force Comfort → voll auf.** Der Sollwert am Thermostat geht auf dessen Maximum, beim
  **Kühlen** spiegelbildlich auf den *niedrigsten* Sollwert (ist „Aus, wenn Raum wärmer als Ziel“
  aktiv und der Raum warm genug, schaltet das Gerät stattdessen ab). Die Ventil-Entity öffnet auf
  `valve_max`, solange der Raum unter dem Ziel liegt; ist er schon warm genug, bleibt sie zu
  (nächster Punkt). Bei **offenem Fenster** wirkt Force Comfort nicht: das Gerät folgt der
  Fensterregel (aus, Fenstertemperatur oder Frostminimum); nur die Kalkschutz-Fahrt läuft weiter.
- **Raum bereits warm genug → Ventil zu.** Liegt die Raumtemperatur auf oder über dem Ziel,
  ist die Öffnung 0.

Ist ein externer Raumsensor konfiguriert und nicht erreichbar, weicht die Ventilberechnung pro
Gerät auf den eigenen Ist-Wert des Thermostats aus — die Position wird weiterhin normal berechnet
und gesendet. Nur wenn auch dieser Wert fehlt, fährt das Ventil ungemoduliert auf 100 % (voll
geöffnet).

---

## 📦 Installation

1. Tempix über den Button oben in HACS öffnen und **Herunterladen**. Ohne Button: HACS → `⋮` → **Benutzerdefinierte Repositories** → `https://github.com/duczz/ha-tempix`, Kategorie **Integration**.
   Ab Home Assistant 2026.11 ersetzt der **Marketplace** HACS: Der Button oben öffnet Tempix dort. Ohne Button: **Einstellungen → Marketplace → Aus Link hinzufügen** → `duczz/ha-tempix` → **Hinzufügen** → **Installieren** (dafür muss GitHub verbunden sein).
2. Home Assistant neu starten
3. **Einstellungen → Geräte & Dienste → + Integration hinzufügen** → `Tempix`

Manuell: den Ordner `custom_components/tempix` nach `<HA-config>/custom_components/tempix` kopieren, dann ab Schritt 2.

---

## ⚡ Schnellstart

### Minimalkonfiguration

Die Konfiguration erfolgt vollständig über den **Config Flow** (Name + 6 Schritte):

1. **Name** vergeben (z.B. `Wohnzimmer`)
2. **Geräte & Sensoren:** TRV(s) (`climate.wohnzimmer_comet_dect`), Raumtemperatursensor (`sensor.wohnzimmer_temperatur`), Fenster- und Außensensor
3. **Klima:** Temperaturen, Modi, Zeitplan-Anpassungen, Kalibrierung
4. **Kalender** oder **Schedule-Helper** konfigurieren
5. **Anwesenheit:** Personen & Präsenz (optional)
6. **Verhalten:** Saison, Abwesenheit, aggressiver Modus
7. **Erweitert:** Ventil, Frost- & Kalkschutz, Timing

> **Ein Thermostat gehört zu genau einem Raum.** Wählst du beim Einrichten ein Thermostat, das ein anderer Tempix-Raum schon steuert, zeigt der Dialog einen Fehler.

**Fertig!** Die Integration erstellt automatisch alle Entities (35+ pro Raum, plus 2 je TRV).

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

| Schicht | Dateien | Aufgabe |
|---|---|---|
| **Typed Config** | `config_model.py` | Einmaliges Parsing beim Start, typisierte Dataclass |
| **Snapshot** | `snapshot_builder.py`, `models/` | Unveränderliche Momentaufnahme aller Eingaben inkl. Sensor-Gesundheit |
| **Policies** | `policies/`, `decision_engine.py` | Ziel bestimmen, Modifikatoren anwenden, Grenzen erzwingen — mit Audit-Trail |
| **Dispatcher** | `hardware_dispatcher.py` | Gerätekommandos: Retry, Ausfallsicherung pro Gerät, Quittungs-Abgleich |
| **Coordinator** | `coordinator.py` + Helfer | State-Listener, Timer, Zusammenspiel der Schichten |
| **Entities** | `climate/sensor/switch/number/select` | 35+ Entities pro Instanz |

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

```yaml
# Template: Heizgrund im Dashboard anzeigen
template:
  - sensor:
      - name: "Wohnzimmer Heizgrund"
        state: "{{ state_attr('climate.wohnzimmer_heizung', 'reason') }}"
```

---

## 📷 Dashboard

> [!NOTE]
> **Entity-IDs sind sprachabhängig.** Home Assistant bildet sie aus dem *übersetzten*
> Anzeigenamen und friert sie bei der Registrierung ein. Das Beispiel zeigt eine deutsche
> Neuinstallation — deine eigenen IDs listet dieser Einzeiler auf
> (Entwicklerwerkzeuge → Template):
> ```jinja
> {{ integration_entities('tempix') | join('\n') }}
> ```

```yaml
type: vertical-stack
cards:
  - type: thermostat
    entity: climate.wohnzimmer_heizung
  - type: entities
    entities:
      - sensor.wohnzimmer_heizung_status
      - binary_sensor.wohnzimmer_heizung_fenster_offen
      - binary_sensor.wohnzimmer_heizung_anwesenheit_personen
      - switch.wohnzimmer_heizung_party_modus
      - switch.wohnzimmer_heizung_comfort_temperatur_erzwingen
      - switch.wohnzimmer_heizung_eco_temperatur_erzwingen
```

---

## 📚 Dokumentation

| Dokument | Zielgruppe |
|---|---|
| [Administrator-Handbuch](.docs/admin_handbuch.md) | Technisch: Architektur, Alle Parameter, Debugging |
| [Endanwender-Handbuch](.docs/endanwender_handbuch.md) | Praktisch: Jede Funktion mit Beispiel & Schritt-für-Schritt |
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

- **Diagnose-Daten:** Einstellungen → Geräte → Tempix → `⋮` → **Diagnose herunterladen**
