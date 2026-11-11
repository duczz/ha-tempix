# 👤 Tempix – Endanwender-Handbuch

---

**Inhalt**

1. [Was macht diese Integration?](#1-was-macht-diese-integration)
2. [Installation in 5 Minuten](#2-installation-in-5-minuten)
3. [Das Virtuelle Thermostat: Dein zentrales Steuerelement](#3-das-virtuelle-thermostat-dein-zentrales-steuerelement)
4. [Funktionen im Überblick](#4-funktionen-im-überblick)
5. [Typische Anwendungsfälle](#5-typische-anwendungsfälle)
6. [Häufige Probleme & Lösungen](#6-häufige-probleme--lösungen)
7. [Best Practices](#7-best-practices)
8. [Profi-Funktionen: JSON-Anpassungen (Adjustments)](#8-profi-funktionen-json-anpassungen-adjustments)
9. [Dashboard-Tipps](#9-dashboard-tipps)

---

## 1. Was macht diese Integration?

Tempix macht deine Heizkörper-Thermostate intelligent. Statt manuell am Thermostat zu drehen, übernimmt Tempix die Steuerung basierend auf:

- ⏰ **Zeitplänen** – Wann soll geheizt werden?
- 👥 **Anwesenheit** – Ist jemand zuhause?
- 🪟 **Fenstern** – Fenster offen? Heizung aus!
- 🌡️ **Außentemperatur** – Automatische Heizpause (Saison Modus)
- 📅 **Kalender** – Homeoffice-Termine steuern die Heizung
- ☀️ **Wetter** – Sonnenschein senkt die Comfort-Temperatur (Sonnenschein-Offset)

> [!NOTE]
> **Ergebnis:** Du sparst Energie, ohne auf Komfort zu verzichten.

---

## 2. Installation in 5 Minuten

### Schritt 1: Installieren
Öffne Tempix in HACS und wähle **Herunterladen**. Ohne Button: HACS → `⋮` →
**Benutzerdefinierte Repositories** → `https://github.com/duczz/ha-tempix`, Kategorie **Integration**.

Ab Home Assistant 2026.11 ersetzt der **Marketplace** HACS. Der Button öffnet Tempix dort. Ohne Button:
**Einstellungen → Marketplace → Aus Link hinzufügen** → `duczz/ha-tempix` → **Hinzufügen** →
**Installieren**. Dafür muss dein GitHub-Konto verbunden sein; Home Assistant fragt beim Hinzufügen danach.
Hast du Tempix schon über HACS installiert, übernimmt der Marketplace es beim ersten Start nach dem Update automatisch.

*Manuell:* Den Ordner `custom_components/tempix` nach `/config/custom_components/tempix/` kopieren.

### Schritt 2: Neustart
Starte Home Assistant neu.

### Schritt 3: Integration hinzufügen

1. Öffne **Einstellungen → Geräte & Dienste**

2. Klicke **+ Integration hinzufügen**

3. Suche nach **"Tempix"**

4. Folge dem **Expert Mode** (6 fokussierte Schritte)

### Schritt 4: Grundkonfiguration

In den sechs Expert-Schritten legst du die Basis:

- **1/6 Geräte & Sensoren:**<br>
  Thermostate, Raumsensoren und Fensterkontakte auswählen.

- **2/6 Klima & Zeitplan:**<br>
  Comfort- & Eco-Temperaturen festlegen, Zeitplanmethode wählen und JSON-Anpassungs-Mappings konfigurieren.

- **3/6 Kalender Oder Zeitpläne:**<br>
  Kalender konfigurieren oder Helfer auswählen (je nach Schritt 2).

- **4/6 Anwesenheit & Personen:**<br>
  Wer beeinflusst die Heizung? (Inkl. Gästemodus).

- **5/6 Verhalten & Regeln:**<br>
  Saison Modus und Detail-Regeln (Absenkung bei Abwesenheit, Manuelle Eingriffe).

- **6/6 Erweitert & Schutz:**<br>
  Ventilsteuerung, Frost- und Kalkschutz.

**Fertig!** 🎉 Deine Heizung ist nun im Automatik-Modus.

---

## 3. Das Virtuelle Thermostat: Dein zentrales Steuerelement

In Home Assistant erscheint für jede Tempix-Instanz ein neues „Klima-Gerät“ (z.B. `climate.wohnzimmer_heizung`). Dies ist dein **virtuelles Thermostat**.

### Warum ist das wichtig?
Das virtuelle Thermostat ist das Gehirn deiner Heizung. Es bündelt alle physischen Heizkörperthermostate (TRVs) eines Raumes und steuert sie intelligent als eine Einheit.

**Die Vorteile:**

- **Zentrale Steuerung:**<br>
  Du musst nicht mehr jedes Thermostat einzeln einstellen. Eine Änderung am virtuellen Thermostat wird sofort an alle Heizkörper im Raum verteilt.

- **Intelligente Korrektur:**<br>
  Tempix berechnet im Hintergrund ständig Offsets und Kalibrierungen. Das virtuelle Thermostat zeigt dir immer die „wahre“ Zieltemperatur an, die du erreichen möchtest, während die echten Thermostate am Heizkörper oft leicht abweichende Werte erhalten, um Energieverluste oder Sensorfehler auszugleichen.

- **Status auf einen Blick:**<br>
  Es zeigt dir nicht nur die Temperatur, sondern in den Attributen auch den Grund (z.B. „Fenster offen“ oder „Party-Modus“), warum gerade eine bestimmte Temperatur gesetzt ist.

### Ein konkretes Beispiel
Stell dir vor, dein **Wohnzimmer** hat zwei Heizkörper: einer unter dem Fenster und einer hinter dem Sofa.

1.  Du stellst die Komfort-Temperatur über **Comfort-Temperatur** auf 21 °C ein.
2.  Tempix misst die Raumtemperatur über einen externen Sensor und gibt beiden Thermostaten dasselbe Ziel vor.
3.  **Das Ergebnis:** Mit der generischen Kalibrierung gleicht Tempix aus, dass jedes Thermostat mit seinem eingebauten Fühler anders misst: Meldet eines wärmer als der Raumsensor (typisch direkt am Heizkörper), stellt Tempix dort entsprechend höher ein.
4.  **Für dich** bleibt es einfach: Das virtuelle Thermostat zeigt dir die berechnete Zieltemperatur und den aktiven Modus an, während Tempix die komplizierte Verteilung im Hintergrund regelt.

> **Hinweis:** Das virtuelle Thermostat (`climate.<name>`) berechnet seinen Zustand normalerweise selbst — deine **dauerhaften** Comfort-/Eco-Sollwerte stellst du weiterhin über die `number.<name>_comfort_temperatur` / `number.<name>_eco_temperatur`-Entities ein. Du kannst aber auch direkt am Climate-Entity drehen: Das startet (genau wie das Drehen am physischen Rad) eine **Temporäre Manuelle Steuerung** — siehe [✋ Manuelle Eingriffe](#-manuelle-eingriffe-override) weiter unten.

### 💡 Best Practice für dein Dashboard: Tempix vs. Hardware-Thermostat

Ein häufiger Fehler ist es, das originale Hardware-Thermostat (z. B. von der Fritzbox oder Klimaanlage) im Dashboard zu verwenden.

**Warum du ausschließlich das Tempix-Thermostat in der UI nutzen solltest:**
- **Vermeidung von Chaos:** Tempix nutzt dynamische Offsets. Wenn du 22 °C im Raum willst, zwingt Tempix die Hardware vielleicht auf 26 °C. Würdest du die Hardware-Entität anzeigen, siehst du "26 °C" und bist verwirrt. Das Tempix-Thermostat zeigt dir immer sauber deine echten 22 °C an.
- **Smarte Status-Attribute:** Nur das Tempix-Thermostat zeigt dir an, *warum* es gerade heizt oder nicht (Attribut `reason: Window Open` oder `reason: Manual Mode`).

**Der perfekte Dashboard-Aufbau:**
1. **Haupt-Element:** Nutze eine Thermostat-Karte (z. B. `simple-thermostat`) verbunden mit dem virtuellen Tempix-Thermostat (z. B. `climate.buero_klimaanlage`) für 99 % deines Alltags (Temperatur einstellen, Heiz-/Kühlmodus wechseln).
2. **Sonder-Modi (Klimaanlagen):** *Entfeuchten* (`dry`) und *Nur Lüfter* (`fan_only`) sind **direkt auf der Tempix-Karte wählbar**, sobald dein Gerät sie meldet — Extra-Buttons aufs Hardware-Thermostat sind dafür nicht nötig. In diesen Modi wird bewusst keine Zieltemperatur gesetzt (Geräte haben dort keine), der Regler ist also ohne Funktion. Als dauerhaften *Zeitplan*-Modus lassen sie sich weiterhin nicht hinterlegen.

---

## 4. Funktionen im Überblick

---

### 🌡️ Comfort & Eco Temperatur

**Was macht es?**
Wechselt zwischen zwei Temperaturstufen je nach Tageszeit.

**Wann benutzt man es?**
Immer – das ist die Kernfunktion.

**Beispiel:**

| Tageszeit | Modus | Temperatur |
|---|---|---|
| 06:00–22:00 | Comfort | 21 °C |
| 22:00–06:00 | Eco | 18 °C |

**So nutzt du es:**
Die Temperaturen kannst du jederzeit über die Number-Entities anpassen:
- `number.<name>_comfort_temperatur`
- `number.<name>_eco_temperatur`

---

### ⏰ Zeitpläne (Schedule Helper)

**Was macht es?**
Steuert wann Comfort und wann Eco gilt.

**Wann benutzt man es?**
Wenn du feste Heizzeiten hast (z.B. morgens und abends Comfort).

**So nutzt du es:**

1. Erstelle einen **Schedule Helper** unter Einstellungen → Helfer

2. Definiere ON-Zeiten = Comfort, OFF-Zeiten = Eco

3. Wähle diesen Schedule in der Tempix-Konfiguration

> [!TIP]
> **Tipp:** Du kannst mehrere Schedules anlegen (z.B. Werktag, Wochenende, Urlaub) und über einen **Input Select** zwischen ihnen wechseln.

---

### 🔄 Automatischer Zeitplan-Wechsel per Kalender

**Was macht es?**
Ein HA-Template-Sensor überwacht deine Kalender und schaltet automatisch auf einen anderen Zeitplan um — z.B. bei Urlaub, Krankheit oder Feiertagen.

**Wann benutzt man es?**
Wenn du mehrere Schedule-Helper hast (z.B. "Normal", "Urlaub", "Homeoffice") und diese abhängig von Kalender-Events automatisch wechseln sollen — ohne jeden Feiertag oder Urlaubstag manuell im Zeitplan anpassen zu müssen.

**So funktioniert es:**

1. Du hast mehrere Schedule-Helper, z.B. `schedule.heizung_normal` und `schedule.heizung_urlaub`
2. Ein **Input Select** (`input_select.heizung_zeitplan`) wählt den aktiven Schedule aus
3. Ein Template-Sensor prüft deine Kalender auf bestimmte Keywords (`urlaub`, `krank`, `homeoffice`) oder Feiertage
4. Eine Automatisierung schaltet den Input Select basierend auf dem Sensor-Status um
5. Tempix liest den jeweils aktiven Schedule über den Input Select

**Beispiel-Vorlage (Template-Sensor):**

```yaml
# In configuration.yaml oder template.yaml
template:
  - trigger:
      - platform: time_pattern
        minutes: "/10"
      - platform: homeassistant
        event: start
      - platform: event
        event_type: event_template_reloaded
    action:
      - service: calendar.get_events
        data:
          start_date_time: "{{ now() }}"
          end_date_time: "{{ now() + timedelta(days=1) }}"
        target:
          entity_id:
            - calendar.mein_kalender        # Persönlicher Kalender
            - calendar.feiertage            # z.B. HA Holiday-Integration
        response_variable: termine
    binary_sensor:
      - name: "Tempix Kalender"
        unique_id: tempix_kalender
        icon: mdi:palm-tree
        state: >
          {% set keywords = ['urlaub', 'krank', 'homeoffice', 'zeitplan'] %}
          {% set ns = namespace(found=false) %}
          {% set today_str = now().strftime('%Y-%m-%d') %}
          {% set now_ts = as_timestamp(now()) %}
          {% if termine is defined %}
            {% for cal_id, cal in termine.items() %}
              {% for event in cal.get('events', []) %}
                {% set start = event.start | string %}
                {% set end   = event.end   | string %}
                {% if start | length == 10 %}
                  {% set active = (start <= today_str) and (today_str < end) %}
                {% else %}
                  {% set active = as_timestamp(start) <= now_ts < as_timestamp(end) %}
                {% endif %}
                {% if active %}
                  {% set summary = (event.summary | default('')).lower() %}
                  {% set is_holiday = cal_id == 'calendar.feiertage' %}
                  {% set keyword_match = keywords | select('in', summary) | list | length > 0 %}
                  {% if is_holiday or keyword_match %}
                    {% set ns.found = true %}
                  {% endif %}
                {% endif %}
              {% endfor %}
            {% endfor %}
          {% endif %}
          {{ ns.found }}
        attributes:
          last_updated: "{{ now().strftime('%d.%m.%Y - %H:%M:%S') }}"
          active_events_details: >
            {% set keywords = ['urlaub', 'krank', 'homeoffice', 'zeitplan'] %}
            {% set ns = namespace(details=[]) %}
            {% set today_str = now().strftime('%Y-%m-%d') %}
            {% set now_ts = as_timestamp(now()) %}
            {% if termine is defined %}
              {% for cal_id, cal in termine.items() %}
                {% for event in cal.get('events', []) %}
                  {% set start = event.start | string %}
                  {% set end   = event.end   | string %}
                  {% if start | length == 10 %}
                    {% set active = (start <= today_str) and (today_str < end) %}
                  {% else %}
                    {% set active = as_timestamp(start) <= now_ts < as_timestamp(end) %}
                  {% endif %}
                  {% if active %}
                    {% set summary_orig  = event.summary | default('Unbenannt') %}
                    {% set summary_lower = summary_orig.lower() %}
                    {% set is_holiday    = cal_id == 'calendar.feiertage' %}
                    {% set keyword_match = keywords | select('in', summary_lower) | list | length > 0 %}
                    {% if is_holiday or keyword_match %}
                      {% set ns.details = ns.details + [{
                        'friendly_name': cal_id,
                        'message':    summary_orig,
                        'start_time': start,
                        'end_time':   end
                      }] %}
                    {% endif %}
                  {% endif %}
                {% endfor %}
              {% endfor %}
            {% endif %}
            {{ ns.details }}
```

> [!NOTE]
> Passe `calendar.mein_kalender` und `calendar.feiertage` auf deine eigenen Kalender-Entity-IDs an. Die Keywords (`urlaub`, `krank`, `homeoffice`, `zeitplan`) kannst du beliebig erweitern.

---

### 📅 Kalender-Steuerung

**Was macht es?**
Dein Google/iCloud/lokaler Kalender steuert die Heizung.

**Wann benutzt man es?**
Wenn du unregelmäßige Zeiten hast (z.B. Homeoffice nur an bestimmten Tagen).

**Beispiel:**
Du hast einen Kalender-Eintrag "Homeoffice" von 08:00–17:00.
- Tempix erkennt das Event und heizt während dieser Zeit auf Comfort
- Mit **Vorlaufzeit** (z.B. 30 min) startet die Heizung schon um 07:30

**So nutzt du es:**

1. In der Konfiguration: Scheduling Mode → "Kalender"

2. Kalender-Entity auswählen.

3. Event-Stichwort eingeben (z.B. "Homeoffice").

4. **Ort-Filter (optional):** Filtert nach dem "Ort"-Feld im Kalender-Event.

5. **Deep Scan (Agenda):** Tempix ruft proaktiv deine Kalender-Agenda ab. So wird auch bei seltenen Abfragen sichergestellt, dass kein Termin (z.B. bei einem Neustart um Mitternacht) übersehen wird.

### 💡 Profi-Befehle im Kalender

Du kannst in der Beschreibung deines Kalender-Events zusätzliche Befehle nutzen:

- `comfort: 21.5` – Überschreibt die konfigurierte Comfort-Temperatur **für die Dauer dieses Termins**
- `eco: 18.0` – Überschreibt die konfigurierte Eco-Temperatur für diesen Termin
- `time: 08:00 - 20:00` – Nur in diesem Zeitraum heizen
- `use_day: sunday` – Nutzt die Heizzeiten eines anderen Wochentags (ideal für Feiertage & Urlaub)
- `use_scheduler: [Name]` – Nutzt einen anderen Zeitplan-Helfer

> [!WARNING]
> **Kalender-Tags haben Vorrang vor deinen konfigurierten Temperaturen.**
>
> Wenn ein aktives Kalender-Event `comfort: 21` in der Beschreibung hat, nutzt Tempix **immer 21°C** — egal was du in der Comfort-Temperatur-Einstellung eingetragen hast. Das erklärt, warum der TRV einen anderen Wert zeigen kann als erwartet.
>
> Tempix zeigt dir das direkt im Status-Sensor: `Comfort (📅 21°C)` — so erkennst du sofort dass ein Kalender-Event die Temperatur überschreibt.
>
> Möchtest du die Tags als Vorlage im Event behalten ohne dass sie greifen, schreibe ein `#` davor: `#comfort: 21` — diese Zeile wird von Tempix ignoriert.

> [!TIP]
> **`use_day` – Referenztag übernehmen**
>
> Mit `use_day` kannst du einem Kalender-Event sagen: „Heize so wie an einem bestimmten Wochentag."
>
> **Schreibweise:** Englisch oder Deutsch, Groß-/Kleinschreibung egal:
> ```
> use_day: sunday      ✓
> use_day: Sunday      ✓
> use_day: sonntag     ✓
> use_day: Sonntag     ✓
> ```
>
> **Beispiel – Urlaub mit Sonntags-Zeitplan:**
> ```
> Titel:       Urlaub
> Ort:         Wohnzimmer, Küche, Schlafzimmer
> Beschreibung: use_day: sonntag
> ```
> Tempix sucht automatisch den passenden Sonntags-Eintrag aus dem Kalender des jeweiligen Raums — du musst keinen Kalender manuell angeben.
>
> **Debugging:** Wenn der Sensor `Active Scheduler Time Period` `00:00 - 23:59 (So.)` anzeigt, ist das Sonntags-Event oder das Event mit dem `use_day`-Tag ganztägig. Fehlt ein passendes Sonntags-Event, zeigt der Sensor die Zeiten des Events mit dem `use_day`-Tag – dann prüfe, ob im Kalender des Raums ein Event am Sonntag mit dem richtigen Ort-Filter vorhanden ist. Tempix schreibt zusätzlich eine einmalige WARNING ins HA-Log (`use_day '<tag>' not found in calendar — falling back to normal schedule`), sobald ein Lookup fehlschlägt — ähnliche Meldungen bei `use_scheduler` und `holiday_use_day`.

> [!TIP]
> **Multi-Raum Unterstützung**
> Du kannst ein einzelnes Kalender-Event für mehrere Räume gleichzeitig nutzen:
> - Im **Kalender-Event**: Schreibe die Räume komma-getrennt in das Feld "Ort" (z.B. `Wohnzimmer, Küche`).
> - In der **Tempix-Konfiguration**: Trage im Feld "Ort-Filter" ebenfalls die Namen (z.B. `Wohnzimmer`) ein.
> Match: Wenn einer der Orte im Kalender-Event mit deinem Ort-Filter übereinstimmt, wird geheizt.

---

### 🗓️ Feiertags-Erkennung

**Was macht es?**
An Feiertagen verhält sich Tempix automatisch wie an einem konfigurierten Wochentag — ohne dass du manuell eingreifen musst.

**Wann benutzt man es?**
Wenn du an Feiertagen (Ostern, Weihnachten, etc.) denselben Zeitplan wie am Wochenende fahren willst, ohne jeden Feiertag manuell im Kalender einzutragen.

**Voraussetzung:**
Die [HA Holiday-Integration](https://www.home-assistant.io/integrations/holiday) installieren und einen Feiertags-Kalender (z.B. `calendar.feiertage_in_deutschland`) anlegen.

**So richtest du es ein:**

In der Tempix-Konfiguration (Schritt Zeitplan):
1. **Feiertags-Kalender:** Wähle deinen Feiertags-Kalender aus (z.B. `calendar.feiertage_in_deutschland`)
2. **Feiertags-Wochentag:** Wähle den Wochentag dessen Zeitplan an Feiertagen gelten soll (z.B. Sonntag)

**Ergebnis:**
- Ist heute ein Feiertag → Tempix verhält sich wie an dem gewählten Wochentag
- Kein Feiertag → normaler Betrieb, keine Auswirkung
- Der Status zeigt `Comfort (Holiday)` oder `Eco (Holiday)` wenn ein Feiertag aktiv ist

> [!TIP]
> **Kalender-Modus:** Der Feiertags-Wochentag überschreibt `use_day` im aktiven Kalender-Event. Du musst keinen `use_day`-Tag im Event eintragen — der Config-Flow-Wert greift automatisch.
>
> **Helper-Modus:** Tempix liest die Heizzeiten des Zieltags aus den Wochentags-Zeiten deines Schedule-Helpers und wendet sie auf heute an; ein Kalender ist dafür nicht nötig (ab Home Assistant 2025.3). Dein Schedule-Helper selbst läuft weiter nach dem echten Wochentag — Tempix ignoriert dessen Zustand an Feiertagen. Sind zusätzlich Kalender hinterlegt, sucht Tempix den Termin des Zieltags zuerst dort; gibt es dort keinen passenden Termin, gilt der Schedule-Helper wie an einem normalen Tag.

---

### 🏖️ Urlaub Zuhause (Holiday-Switch)

**Was macht es?**
Ein Schalter pro Raum, der genau denselben Wochentag-Ersatz auslöst wie ein echter Feiertag — nur manuell statt automatisch über den Feiertagskalender.

**Wann benutzt man es?**
Wenn du Urlaub hast, aber zuhause bist: kein Feiertag, kein Kalender-Event nötig — einfach den Schalter aktivieren, und Tempix fährt für diese Tage den Wochenend-Zeitplan. Funktioniert unabhängig davon, ob du Scheduler-Helper oder Kalender nutzt.

**Voraussetzung:**
Wie bei der Feiertags-Erkennung muss ein Zieltag konfiguriert sein — **Feiertags-Wochentag** (`holiday_use_day`) reicht dafür aus. Ist er leer, hat der Schalter keinen Effekt, und Tempix schreibt einmalig eine WARNING ins HA-Log.

**So richtest du es ein:**

1. In der Tempix-Konfiguration (Schritt Zeitplan) **Feiertags-Wochentag** setzen, falls noch nicht geschehen (z.B. Sonntag) — ein Feiertags-Kalender ist dafür **nicht** nötig.
2. Den Schalter **Urlaub Zuhause** in Home Assistant aktivieren (auf der Geräteseite des Raums; die Entity-ID entsteht bei der Registrierung aus dem Bereichsnamen (falls das Gerät einem Bereich zugeordnet ist), dem Namen deines Eintrags und dem Schalternamen, z.B. `switch.wohnzimmer_heizung_urlaub_zuhause`), sobald der Urlaub beginnt, und danach wieder deaktivieren.

**Ergebnis:**
- Schalter an → Tempix verhält sich wie an dem gewählten Wochentag, ganz gleich welcher Wochentag heute wirklich ist
- Schalter aus → normaler Betrieb
- Der Status zeigt wie bei einem echten Feiertag `Comfort (Holiday)` bzw. `Eco (Holiday)`

> [!TIP]
> **Eigener Wochentag für Urlaub (optional):** Ein echter Feiertag ist meist ein Einzeltag mitten in der Woche — am nächsten Tag geht's wieder zur Arbeit, das Verhalten ähnelt eher einem Sonntag. Ein Urlaub zuhause dauert dagegen oft mehrere Tage am Stück, ohne Arbeits-Constraint am Folgetag — das ähnelt eher einem Samstag. Wer das unterscheiden will, trägt zusätzlich **Feiertags-Wochentag (Urlaub Zuhause)** (`vacation_home_use_day`) ein, z.B. Samstag. Dieses Feld gilt **ausschließlich**, solange der Schalter aktiv ist, und überschreibt dann `holiday_use_day`. Echte Feiertage über den Kalender bleiben davon unberührt und nutzen weiterhin nur `holiday_use_day`. Bleibt das Feld leer, verhält sich der Schalter wie bisher (Fallback auf `holiday_use_day`).
>
> **Nicht verwechseln mit „Urlaub Abwesend" (`vacation_mode_switch`):** Dieser Modus hält nur eine feste Zieltemperatur, unabhängig vom Wochentag. „Urlaub Zuhause" ändert stattdessen den Zeitplan selbst. **Sind beide Schalter gleichzeitig an, gilt „Urlaub Abwesend"** — „Urlaub Zuhause" hat dann keinen Einfluss auf die Zieltemperatur (u. a. haben Fenster offen, manueller Eingriff, Entkalkung und eine ausgeschaltete Automatik ihrerseits weiterhin Vorrang).
>
> **Mehrere Räume gleichzeitig:** Der Schalter ist bewusst pro Raum, damit z.B. ein Gästezimmer normal weiterlaufen kann. Willst du alle Räume auf einmal umschalten, reicht eine kleine Automation:
> ```yaml
> automation:
>   - alias: "Urlaub zuhause: alle Räume auf Wochenend-Zeitplan"
>     triggers: []  # z.B. per input_boolean oder manuell ausgelöst
>     actions:
>       - action: switch.turn_on
>         target:
>           entity_id:
>             # Entity-IDs an deine Installation anpassen (siehe Geräteseite)
>             - switch.wohnzimmer_heizung_urlaub_zuhause
>             - switch.kueche_heizung_urlaub_zuhause
>             - switch.schlafzimmer_heizung_urlaub_zuhause
> ```

---

### 👥 Personen & Geräte (Anwesenheit)

**Was macht es?**
Wenn niemand zuhause ist, senkt Tempix die Temperatur automatisch ab.

> [!IMPORTANT]
> **Abwesenheit wirkt nur in der Comfort-Phase.** Egal welches der vier Verhalten du unten wählst: Tempix wertet Abwesenheit ausschließlich aus, solange dein Zeitplan oder Kalender gerade eine Comfort-Phase fährt (oder eine unmittelbar bevorsteht und vorgeheizt wird). Läuft gerade die Eco-Phase, ändert deine Abwesenheit **nichts** — es bleibt bei der Eco-Temperatur. Auch *Komplett ausschalten* schaltet dann nicht aus.

**Wann benutzt man es?**
Wenn du tagsüber arbeitest oder unregelmäßig unterwegs bist.

> [!TIP]
> **Nur Anwesenheit, kein Zeitplan?** Das macht Sinn wenn du keinen festen Tagesablauf hast — z.B. Schichtarbeit, Homeoffice mit wechselnden Zeiten oder eine Ferienwohnung. Tempix heizt dann immer genau dann auf Comfort, wenn jemand da ist — egal um welche Uhrzeit. Wer um 2 Uhr nachts heimkommt, findet eine warme Wohnung vor.

**Beispiel:**

| Situation | Temperatur |
|---|---|
| Jemand zuhause + Comfort-Zeit | 21 °C |
| Niemand zuhause + Comfort-Zeit | 21 °C − 2 °C = **19 °C** |
| Niemand zuhause + Eco-Zeit | 18 °C |

**So nutzt du es:**

1. **Personen & Geräte** auswählen (z.B. `person.martin` oder Tracker)

2. **Abwesenheits-Offset** einstellen — in der Konfiguration unter **5/6 Verhalten & Regeln**, nicht im
   Dashboard (z.B. 2 °C Absenkung). Wirksam wird er nur, wenn **Verhalten bei Abwesenheit** auf
   *Temperatur absenken (Offset)* steht.

3. Optional: **Näherungs-Sensor** (Schritt **4/6 Anwesenheit & Personen**, Bereich *Näherung & Geo-Fencing*) –
   ein Gerät der HA-Integration *Proximity*. Ist jemand auf dem Heimweg, kann Tempix schon vor der Ankunft
   aufheizen. Der **Aktivierungs-Radius** legt die Distanz fest (Standard 500 m); mit der
   **Bestätigungs-Dauer** zählt „angekommen“ bzw. „auf dem Heimweg“ erst nach dieser Zeit, das dämpft GPS-Flackern.

**Status-Anzeige bei Abwesenheit:**

Tempix zeigt dir im Status-Sensor direkt an, was gerade passiert:

- **Eco:**<br>
  Normaler Sparbetrieb (auch wenn du abwesend bist).

- **Away (🚶 -2.0°C):**<br>
  Du bist abwesend, aber der Zeitplan ist aktiv — der Away-Offset wird vom Comfort-Sollwert abgezogen. Bei Away-Verhalten „Eco“ steht `Away (🚶 Eco)`, die Zieltemperatur ist dann die Eco-Temperatur.

### Anwesenheit erzwingt Komfort (Presence Forces Comfort)

**Funktionsweise:**<br>
Dieses Feature ermöglicht es, dass die Heizung in den Comfort-Modus wechselt, sobald jemand zu Hause ist, selbst wenn der reguläre Zeitplan oder Kalender eine Eco-Phase (Absenkung) vorsieht.

Du kannst dieses Verhalten zusätzlich auf ein bestimmtes **Zeitfenster** (z.B. 08:00 bis 22:00 Uhr) einschränken. Außerhalb dieses Fensters (z.B. nachts) folgt die Heizung wieder strikt deinem normalen Zeitplan, auch wenn du anwesend bist.

> [!TIP]
> In den Experten-Einstellungen (Schritt 5) kannst du über das Dropdown **Verhalten bei Abwesenheit** (`away_behavior`) genau festlegen, was passieren soll, wenn niemand zuhause ist — **innerhalb einer laufenden Comfort-Phase**, siehe den Kasten weiter oben:
> 1. **Temperatur absenken (Offset):** Zieht den `away_offset` von der Comfort-Temperatur ab (beim Kühlen wird stattdessen aufgeschlagen — die Richtung passt sich an).
> 2. **In Eco-Modus wechseln:** Setzt die Eco-Temperatur statt der Comfort-Temperatur. Die Comfort-Phase läuft dabei weiter, nur der Sollwert wechselt.
> 3. **Komplett ausschalten:** Schaltet das Thermostat komplett AUS.
> 4. **Abwesenheit ignorieren:** Ignoriert die Abwesenheit komplett und folgt stur dem aktiven Zeitplan.

---

### 🪟 Fenster-Erkennung

**Was macht es?**
Wenn ein Fenster geöffnet wird, senkt Tempix sofort die Temperatur oder schaltet den TRV ab.

**Wann benutzt man es?**
Immer, wenn du Fenstersensoren hast.

**Beispiel:**
- Fenster auf → Temperatur sinkt auf die **Lüftungs-Temperatur** (z.B. 5 °C oder 0 = aus)
- Fenster zu → nach der eingestellten Schließ-Verzögerung rechnet Tempix neu aus, was jetzt gelten soll (meist der vorherige Wert)

**So nutzt du es:**
Binary Sensors der Fenster in der Konfiguration angeben.

> [!TIP]
> **Das Fenster gewinnt — auch gegen „Comfort erzwingen".** Steht 🔥 Force Comfort an und du
> öffnest ein Fenster, schaltet Tempix trotzdem ab. Einzige Ausnahme ist die **Kalkschutz-Fahrt** — die muss laufen,
> sonst setzt sich das Ventil fest.

> [!TIP]
> **Mehrere Fenstersensoren:** Fällt ein Sensor aus (leere Batterie), stoppt Tempix nicht mehr die gesamte Erkennung. Solange mindestens die Hälfte der Sensoren erreichbar ist, entscheiden die erreichbaren. Meldet einer davon „offen", gilt das Fenster als offen.

> [!NOTE]
> Beim Schließen eines Fensters rechnet Tempix den richtigen Zustand neu aus, statt einen früheren
> zurückzuspielen. So kommt nichts Veraltetes zurück – etwa eine Party-Temperatur, deren Timer
> inzwischen abgelaufen ist.

> [!TIP]
> **Fenster kurz wieder aufgemacht?** Öffnest du das Fenster während der Schließ-Verzögerung erneut
> (oder in einem Raum mit mehreren Fenstersensoren ein anderes Fenster), gilt es ohne Lücke weiter als
> offen – der Raum heizt oder kühlt zwischendurch nicht. Ein Kontakt, der nur ganz kurz „offen" meldet
> (kürzer als die Öffnungs-Verzögerung, z. B. durch Prellen), startet beim Schließen keine
> Schließ-Verzögerung.

---

### ✈️ Urlaub Abwesend (Urlaubs-Modus)

**Was macht es?**
Setzt eine feste, niedrige Temperatur, solange du im Urlaub bist – tiefer als Eco, aber noch frostsicher. Der Schalter gilt pro Raum: Schalte ihn in jedem Raum ein, der absenken soll.

**Wann benutzt man es?**
Längere Abwesenheit (Urlaub, Dienstreise), wenn man Energie sparen will aber die Rohre nicht einfrieren sollen.

**Verhalten:**
- Übersteuert alles (Kalender, Scheduler, Anwesenheit)
- Frostschutz greift weiterhin als Untergrenze
- Temperatur frei einstellbar (Standard: 15 °C)
- Hat Vorrang vor „Urlaub Zuhause": sind beide Schalter an, gilt dieser Modus

**So nutzt du es:**

1. **Urlaub Abwesenheitstemperatur** auf gewünschte Temperatur setzen (z.B. 15 °C)
2. Schalter **Urlaub Abwesend** einschalten → Urlaub aktiv
3. Nach Rückkehr Switch ausschalten → normaler Betrieb

**Alternativ via Kalender:**
```
Urlaub
vacation: 15
```
Trage `vacation: 15` in die Beschreibung deines Urlaubs-Events ein. Tempix erkennt den Tag und aktiviert automatisch „Urlaub Abwesend" mit der angegebenen Temperatur — der Status wechselt auf **Vacation Mode** (identisch zum Switch).

---

### 🎉 Party-Modus

**Was macht es?**
Setzt eine feste Temperatur, unabhängig von Zeitplänen. Der Schalter gilt pro Raum; der Service `tempix.set_party_mode` wirkt ohne Raumangabe auf alle Räume.

**Wann benutzt man es?**
Gäste da, Feier, gemütlicher Abend.

**Beispiel:**
```
Party-Modus EIN → Raum auf 22 °C
Nach 4 Stunden → automatisch zurück zum normalen Betrieb
```

**So nutzt du es:**

- Switch **Party-Modus** einschalten
- Oder per Service mit Timer: `tempix.set_party_mode`

---

### 👤⭐ Gäste-Modus

**Was macht es?**
Simuliert Anwesenheit auch wenn die Hauptbewohner weg sind.

**Wann benutzt man es?**
Wenn Gäste alleine zuhause sind und die Anwesenheitserkennung sie nicht kennt.

**So nutzt du es:**
Switch **Gäste-Modus** aktivieren.

---

### ❄️ Frostschutz

**Was macht es?**
Verhindert, dass Rohre einfrieren – hält Mindesttemperatur.

**Wann benutzt man es?**
Als Sicherheitsnetz, besonders im Urlaub oder in selten beheizten Räumen.

**Wichtig:** Der Frostschutz ist in erster Linie eine Untergrenze, keine eigene Betriebsart — er hebt den Sollwert an, solange die Automatik aktiv ist. Zusätzlich hat er eine eigene Notfall-Funktion: Ist die Automatik komplett aus (Saison-Schalter, Master-Schalter oder ein ausgefallener Außentemperatur-Sensor) UND kühlt der Raum trotzdem unter die Frostschutz-Temperatur ab, schaltet Tempix die Heizung aktiv ein, bis der Raum wieder sicher ist. Ein offenes Fenster gewinnt weiterhin gegen den Frostschutz.

**Beispiel:**
Saison-Modus an, Temperatur fällt unter 5.0 °C → Tempix heizt automatisch auf 5.0 °C – auch wenn der Außentemperatur-Sensor gerade ausgefallen ist. Ist der Saison-Modus bewusst aus (z. B. im Sommer) und die Raumtemperatur sinkt dennoch unter 5.0 °C (z. B. ein ungeheizter Keller in einer Kälteperiode), schaltet Tempix trotzdem kurz ein, bis die Gefahr vorbei ist — ein offenes Fenster verhindert das weiterhin.

**So nutzt du es:**

1. In der Konfiguration den **Frostschutz** aktivieren.

2. **Frostschutz-Temperatur** festlegen (Standard: 5 °C).

---

### 🧭 Kalibrierung

**Was macht es?**
Gleicht die Differenz zwischen TRV-Sensor und deinem externen Sensor aus.

**Wann benutzt man es?**
Wenn dein TRV z.B. 23 °C anzeigt, dein externer Sensor aber 21 °C misst. Tempix korrigiert den TRV-Offset, damit die echte Raumtemperatur stimmt.

**Zwei Modi:**

| Modus | Beschreibung | Geeignet für |
|---|---|---|
| **Native** | Setzt das Offset-Attribut des TRV direkt | Tado, moderne ZigBee TRVs |
| **Generisch** | Addiert den Offset auf die Soll-Temperatur – funktioniert auch ohne Kalibrierungs-Entity | FRITZ!DECT und alle anderen TRVs |

**Wie funktioniert Generisch?**
Tempix misst die Differenz zwischen dem internen TRV-Sensor und deinem externen Raumsensor und addiert diesen Wert auf die gesendete Soll-Temperatur. So "glaubt" der TRV, er muss höher heizen – und öffnet das Ventil.

**Beispiel FRITZ!DECT:**
- TRV-Sensor intern: 23,5 °C, Raumsensor: 20,9 °C → Offset: **2,5 °C**
- Comfort-Sollwert: 20,7 °C → gesendet an TRV: 20,7 + 2,5 = **23,2 °C**
- Ergebnis: TRV öffnet das Ventil voll, weil sein interner Sensor (23,5 °C) unter dem Ziel liegt

> **Tipp:** Im Diagnose-Panel ist `last_calibrations: {}` bei FRITZ!DECT normal – der Offset wirkt trotzdem über `last_generic_offsets`. Dort siehst du den aktuellen Offset-Wert.

> [!NOTE]
> Meldet dein Thermostat keine interne Ist-Temperatur mehr (z. B. FRITZ-Sommerbetrieb oder Funkverbindung weg), kann die generische Kalibrierung nicht arbeiten – der Raum heizt dann mit dem unkorrigierten Sollwert weiter. Passiert das länger als 2 Stunden, während der Raum aktiv heizt oder kühlt, erscheint automatisch eine Meldung unter **Einstellungen → System → Reparaturen**. Sie verschwindet von selbst, sobald das Thermostat wieder Werte liefert.

---

### ☀️ Sonnenschein-Offset

**Was macht es?**
Bei sonnigem Wetter (`sunny`) reduziert Tempix die Comfort-Temperatur etwas – denn Sonneneinstrahlung heizt den Raum passiv mit.

**So nutzt du es:**

1. Wetter-Entity in der Konfiguration angeben (separates Feld `weather_entity`).
2. Switch **Sonnenschein-Offset** aktivieren.

**Wichtige Hinweise:**
- Der Offset gilt nur im **Comfort-Modus** – Eco bleibt unverändert. Ausnahme: Steht das Away-Verhalten auf „Eco“ und bist du abwesend, arbeitet Tempix im Comfort-Zweig mit der Eco-Temperatur; der Offset wird dann von dieser abgezogen (Status: `Away (🚶 Eco, ☀️ -1.0°C)`).
- Nur `sunny` löst die Absenkung aus — `clear`, `clear-night` und andere Zustände nicht.
- Status-Sensor zeigt z.B. `"Comfort (☀️ -0.5°C)"` wenn aktiv.

**Tipp: Live-Reaktion**<br>
Dieser Schalter (wie alle Tempix-Schalter) reagiert sofort beim Ein- und Ausschalten – kein Warten auf den nächsten Heartbeat.


---

### 🚀 Aggressiver Modus (Push-To-Target)

**Was macht es?**
Wenn die Raumtemperatur sehr weit vom Ziel entfernt ist (z.B. nach dem Lüften oder bei der Rückkehr aus dem Urlaub), "pusht" Tempix die Heizung kurzzeitig über das Ziel hinaus, um den Aufheizprozess zu beschleunigen.

**Beispiel:**
- Ziel: 21 °C | Raum: 17 °C
- Tempix setzt Soll-Temperatur kurzzeitig auf 22 °C (Ziel + Offset 1 °C), bis der Raum warm genug ist, um das Ventil voll zu öffnen und schnell Energie einzubringen.

**So nutzt du es:**

1. **In der Konfiguration:**<br>
   **Aggressiver Modus** wählen.

2. **Reichweite:**<br>
   Ab wie vielen Grad Differenz (z.B. 2 °C) soll der Modus starten?

3. **Offset:**<br>
   Wie viele Grad (z.B. 1 °C) soll zusätzlich zum Ziel geheizt werden?

---

### 🚿 Entkalkungsschutz

**Was macht es?**
Fährt den TRV regelmäßig einmal kurz hoch, um Ventilverkalkung zu verhindern.

**Wann benutzt man es?**
Bei TRVs die lange in einer Position stehen (z.B. im Sommer).

**So nutzt du es:**

Switch **Entkalkungsschutz** aktivieren und Tag/Uhrzeit in der Konfiguration festlegen.

**Hinweis:**<br>
Der Entkalkungsschutz ist standardmäßig **ausgeschaltet**, um ungewollte Ventilbewegungen zu vermeiden.

---

---

### 🔧 Ventilsteuerung (optional)

**Was macht es?**
Bietet ein Thermostat die Ventilöffnung in Prozent an (eine `number`-Entity am Gerät), kann Tempix die Öffnung selbst steuern – proportional zum Abstand zur Zieltemperatur.

**So nutzt du es:**
In Schritt **6/6 Erweitert & Schutz**, Bereich *Ventilsteuerung*, unter **Ventilsteuerung / Kennlinie** wählen: *Aus*, *Linear*, *Pessimistisch* (öffnet früher) oder *Optimistisch* (öffnet später). Findet Tempix am Gerät keine passende Entity, passiert nichts.

Im selben Bereich stellst du fünf weitere Werte ein:

- **Temperaturdifferenz für volle Öffnung** (Standard 1,0 °C): Abstand zur Zieltemperatur, ab der das Ventil ganz öffnet.
- **Ventil-Schrittweite** (Standard 10 %): In diesen Prozentschritten wird die Öffnung angepasst.
- **Mindestöffnung (Schließpunkt)** (Standard 0 = aus): Bleibt dein Ventil bis zu einer bestimmten Öffnung zu (z. B. bis 18 %), trag diesen Wert ein. Dann wirkt schon die kleinste Öffnung, die Tempix sendet. Zu (0 %) bleibt zu.
- **Maximale Öffnung** (Standard 100 %): Mehr als das öffnet Tempix das Ventil nie.
- **Ventil-Entitäts-Stichwort** (Standard `valve`): Tempix steuert nur Entities, deren Name dieses Stichwort enthält. Ändere es nur, wenn Tempix die falsche oder keine Entity findet.


---

### ⚖️ Hysterese

**Was macht es?**
Legt ein Toleranzband fest, das nur aktiv ist wenn du **„TRV bei Raumtemperatur abschalten"** (`off_if_above_room_temp`) aktiviert hast.

- TRV schaltet **ab** wenn Raumtemperatur ≥ Sollwert + Hysterese
- TRV schaltet **ein** wenn Raumtemperatur < Sollwert − Hysterese

**Ohne `off_if_above_room_temp`** hat die Hysterese keinen Effekt auf die Heizung.

> **Tipp für Gas- & Wärmepumpen-Nutzer:** Wenn du `off_if_above_room_temp` oder `HVAC-Modus Eco = Aus` verwendest, empfiehlt sich eine Hysterese von mindestens **0.5 °C** — das schützt dein Gerät vor zu häufigem Ein- und Ausschalten.

**So nutzt du es:**

Die Hysterese ist ein **Konfigurations-Wert, keine Entity** — du findest sie in der
Tempix-Konfiguration unter **2/6 Klima & Zeitplan → Hysterese** (Standard: **0.3 °C**,
einstellbar von 0,1 bis 2 °C). Im Dashboard taucht sie nicht auf.

---

### 🌡️ Smart Pre-Conditioning (Aufheiz-Optimierung)

**Was macht es?**
Tempix lernt, wie schnell sich dein Raum aufheizt. Basierend darauf wird der Heizvorgang automatisch so vorverlegt, dass die Comfort-Temperatur exakt zum geplanten Zeitpunkt erreicht ist.

**Wann benutzt man es?**
Wenn es morgens oder zum Feierabend pünktlich warm sein soll, ohne dass die Heizung stundenlang vorher auf Verdacht läuft.

**So nutzt du es:**

1. In den Einstellungen **Smart Pre-Conditioning** aktivieren.

2. **Max. Vorlaufzeit:** Legt fest, wie viele Stunden Tempix maximal früher anfangen darf zu heizen (Standard: 2h).

3. **Wichtig:**<br>
   Ist das Feature **AUS**, startet die Heizung exakt zum Zeitpunkt des Kalender-Events/Zeitplans (0 Minuten Vorlauf).

4. **Fallback:**<br>
   Ist das Feature **AN**, aber es liegen noch keine gelernten Daten vor, nutzt Tempix einen Standardwert von **1,0 °C pro Stunde** (begrenzt durch deine maximale Vorlaufzeit).

---

### 📅 Kalender: Tägliche Zeitfenster

**Was macht es?**
Wenn du ein mehrtägiges Ereignis im Kalender hast (z. B. "Urlaub"), heizt Tempix normalerweise rund um die Uhr auf Comfort-Temperatur. Mit einem Zeitfenster kannst du das auf bestimmte Stunden pro Tag begrenzen.

**So nutzt du es:**

Schreibe einfach `time: HH:MM - HH:MM` in die Beschreibung deines Kalender-Termins.

**Beispiel:**
```text
Urlaub
comfort: 21.0
time: 08:00 - 22:00
```
- **Ergebnis:** Tempix heizt jeden Tag des Urlaubs von 08:00 bis 22:00 Uhr auf 21°C. Außerhalb dieser Zeit (nachts) wird automatisch auf die Eco-Temperatur abgesenkt.
- **Smart Pre-Conditioning:** Auch bei täglichen Zeitfenstern funktioniert das Vorheizen – die Heizung ist also pünktlich um 08:00 Uhr warm.
- **Gut zu wissen:** Schreibe `23:59` statt `24:00` – mit `24:00` wird das Zeitfenster ignoriert. Ein Fenster über Mitternacht (z. B. `22:00 - 06:00`) funktioniert. Das Zeitfenster wirkt nur in Räumen, die im Kalender-Modus laufen.
- **Manuell verstellt?** Drehst du innerhalb des Termins am Thermostat, gilt das bis zum nächsten Fensterbeginn oder Fensterende – im Beispiel also bis 22:00 Uhr oder bis 08:00 Uhr. Danach übernimmt der Zeitplan wieder. Ausnahme: am letzten Tag des Termins nach dem Fensterende gibt es keinen nächsten Fensterbeginn mehr; dann gilt deine Änderung länger, höchstens 24 Stunden.

---

### ✋ Manuelle Eingriffe (Override)

**Was passiert, wenn ich am Thermostat drehe, die Temperatur im HA-Dashboard ändere oder direkt am Tempix-Thermostat einstelle?**

Tempix erkennt das sofort! Durch das smarte **Echo-Tracking** passiert folgendes:

- **Temporäre Manuelle Steuerung:**<br>
  Tempix wechselt automatisch in den Status *Temporäre Manuelle Steuerung*. Die Automatisierung pausiert für diesen Raum und überlässt dir die volle Kontrolle. Die Temperatur wird **nicht** mehr starr auf den Zeitplan zurückgezwungen. (Nicht zu verwechseln mit dem Schalter **Manueller Modus**, der kompletten Pause, siehe unten.)

- **Option ausgeschaltet:**<br>
  Ist in den Raum-Optionen **„Temporären manuellen Modus erlauben"** aus, übernimmt Tempix eine Änderung an der Karte oder am Schalter *Temporäre Manuelle Steuerung* nicht. Du bekommst dann die Meldung *„Der temporäre manuelle Modus ist für … deaktiviert. Aktiviere zuerst ‚Temporären manuellen Modus erlauben' in den Optionen."* Im **Manuellen Modus** gehen Änderungen an der Karte trotzdem direkt ans Gerät.

- **Nur die Temperatur verstellt, während der Raum aus ist:**<br>
  Hat die Automatik den Raum gerade aus (z. B. Eco-Modus „Aus" bei einer Klimaanlage) und du verstellst auf der Karte **nur die Temperatur**, bleibt das Gerät aus. Tempix merkt sich den Wert und zeigt ihn an; er gilt, sobald du auf der Karte den Modus einschaltest (z. B. „Kühlen"). Willst du sofort einschalten, wähle den Modus mit. Beim nächsten Zeitplanwechsel verfällt der gemerkte Wert wie jeder Override.

- **Auto-Resume (Auto-Reset):**<br>
  Sobald sich die Zeitplan-Lage logisch ändert, beendet Tempix die temporäre Steuerung automatisch und übernimmt wieder das Ruder. Das passiert, wenn die nächste reguläre Phase beginnt (z.B. der geplante Wechsel von "Eco" auf "Comfort"), wenn ein anderer Zeitplan aktiv wird, wenn eine Anpassungs-Regel greift oder wegfällt — **und auch, wenn dein Abwesenheits-Status kippt** (jemand kommt heim, oder der Letzte geht). Spätestens nach 24 Stunden endet die temporäre Steuerung in jedem Fall. Du kannst sie auch jederzeit manuell über den Schalter **Temporäre Manuelle Steuerung** beenden — oder umgekehrt **einschalten**, um deine gerade angezeigte Solltemperatur sofort als Override "einzufrieren", ganz ohne ans Thermostat zu fassen.

> [!NOTE]
> **Sonderfall Pause-Modus:** Ist der Schalter **Manueller Modus** (die komplette Pause, siehe unten) aktiv, verhält sich das Tempix-Thermostat wie eine reine Fernbedienung: Jede Änderung, die du daran vornimmst, wird 1:1 direkt an die angeschlossenen Heizkörper-Thermostate weitergereicht. Es entsteht dabei **kein** Temporary Override, da die gesamte Automatik ja ohnehin pausiert ist.

> [!IMPORTANT]
> **Sicherheit geht vor:** Ein temporärer manueller Eingriff überschreibt reguläre Zeitpläne, Abwesenheiten und sogar „Urlaub Abwesend". **Kritische Sicherheitsfunktionen** (wie die Fenster-Offen-Erkennung oder der Frostschutz) bleiben jedoch strikt aktiv und übersteuern deinen manuellen Eingriff sofort, um Energieverschwendung oder Schäden zu verhindern. Dein Eingriff wird dabei nur **pausiert, nicht gelöscht**: Schließt du das Fenster wieder, gilt deine manuell gewählte Temperatur automatisch weiter.

> [!NOTE]
> **Die Anzeige "springt zurück"? Das ist Absicht.** Wenn du an der Tempix-Karte eine Temperatur einstellst, während gerade eine Schutzfunktion aktiv ist (Fenster offen, Frostschutz), zeigt die Karte kurz deinen Wunschwert und springt dann sichtbar auf den tatsächlich geltenden Wert zurück. So siehst du sofort, dass gerade eine Schutzfunktion regiert — kein Fehler, kein Bug.

> [!NOTE]
> **Am Thermostat gedreht, und die Anzeige springt weiter? Auch das ist Absicht.** Mit der Kalibrierung **Generisch** oder dem **Aggressiven Modus** zeigt das Thermostat nicht die Raum-Zieltemperatur, sondern einen umgerechneten Wert. Stellst du am Gerät einen Wert ein, versteht Tempix ihn als gewünschte **Raum**temperatur und rechnet die Korrektur wieder obendrauf — die Anzeige am Gerät springt danach auf einen anderen Wert. Stelle also die Temperatur ein, die du im Raum haben möchtest, und drehe nicht „ein bisschen weniger als angezeigt": Das kann den Raum sogar wärmer machen. Soll das Thermostat genau deinen Wert behalten, wähle für den Raum die Kalibrierung **Native** oder **Aus** und schalte den Aggressiven Modus ab.

> [!WARNING]
> **Neustart-Sicherheit (Reboot):** Dein manueller Eingriff am Thermostat wird sicher gespeichert und ist auch nach einem Neustart von Home Assistant oder einem Stromausfall noch für die Restlaufzeit aktiv.

> [!NOTE]
> **Einschränkung bei Netzwerkausfall:** Wenn du den Thermostat während eines WLAN-Ausfalls manuell verstellst, erkennt Tempix diese Änderung nicht. Nach dem Reconnect überschreibt Tempix den manuell gesetzten Wert mit der laut Plan korrekten Temperatur.

**Wie kann ich einen bestimmten Modus über die App erzwingen?**
Du kannst im Dashboard zwei spezielle Schalter nutzen, um die Automatik sofort zu überschreiben:

1. **Comfort-Temperatur erzwingen** (🔥): Setzt die Heizung sofort und dauerhaft auf **Comfort**.

2. **Eco-Temperatur erzwingen** (🍃): Setzt die Heizung sofort und dauerhaft auf **Eco**.
*Hinweis:* Solange einer dieser Schalter aktiv ist, pausiert der Zeitplan vollständig in diesem Raum.

---

### 🏖️ Saison Modus

**Wie schalte ich die Heizung im Sommer aus?**

Es gibt zwei Wege:

1. **Saison Modus:**<br>
   Verknüpfe einen Schalter (z.B. `input_boolean.heizperiode`). Ist dieser AUS, regelt Tempix nicht mehr nach Zeitplan: Die Heizung geht aus (oder auf die **Ruhetemperatur**, falls eine eingestellt ist). Nur der Frostschutz-Notfall greift weiter.

2. **Außentemperatur:**<br>
   Tempix kann die Heizung automatisch deaktivieren, wenn es draußen warm genug ist (z.B. > 17 °C).

#### Hysterese (Außentemperatur-Schwelle)

Damit die Anlage nicht ständig an- und ausgeht, wenn die Außentemperatur um den Schwellenwert pendelt, gibt es eine einstellbare **Hysterese**. Tempix unterscheidet hierbei intelligent zwischen **Heizen** und **Kühlen** (z.B. bei Klimaanlagen).

**Beispiel Heizen (Winter):**
Schwelle: **17 °C** | Hysterese: **1 °C**
- Heizung schaltet **ab** bei 18 °C (Schwelle + Hysterese)
- Heizung schaltet **wieder ein** bei 16 °C (Schwelle − Hysterese)

**Beispiel Kühlen (Sommer):**
Schwelle: **24 °C** | Hysterese: **1 °C**
- Kühlung schaltet **ein** bei 25 °C (Schwelle + Hysterese)
- Kühlung schaltet **wieder ab** bei 23 °C (Schwelle − Hysterese)

**So nutzt du es:**
Den Wert findest du in der Konfiguration unter **1/6 Geräte & Sensoren → Außentemperatur → Hysterese (Außenschwelle)**.

---

### ✋ Manuelle Unterbrechung (Pause)

**Was macht es?**
Schaltet Tempix **komplett aus** – keine Temperaturänderungen, kein HVAC-Mode, keine Kalibrierung, kein Frostschutz. Du hast volle manuelle Kontrolle über den Thermostat.

**Wann benutzt man es?**
Wenn es dir z.B. zu warm ist und du die Temperatur am Thermostat selbst einstellen willst, ohne dass Tempix dazwischenfunkt.

**So nutzt du es:**
1. **Manueller Modus** (✋) einschalten → Tempix greift nicht mehr ein
2. Thermostat manuell einstellen wie gewünscht
3. Wenn alles passt: Switch wieder ausschalten → Tempix übernimmt sofort wieder

> [!WARNING]
> Während der Pause ist auch der **Frostschutz deaktiviert**! Vergiss nicht, den Schalter wieder auszuschalten – besonders im Winter.

---

### 🔥🍃 Force Comfort / Force Eco

**Was macht es?**
Überschreibt den Zeitplan sofort und dauerhaft mit Comfort- oder Eco-Temperatur.

**Wann benutzt man es?**
Wenn du die Automatik temporär umgehen willst, z.B. abends länger heizen oder tagsüber Energie sparen.

**So nutzt du es:**
- **Comfort-Temperatur erzwingen** (🔥): Setzt die Heizung sofort auf **Comfort**.
- **Eco-Temperatur erzwingen** (🍃): Setzt die Heizung sofort auf **Eco**.

> [!WARNING]
> Solange einer dieser Schalter aktiv ist, pausiert der Zeitplan vollständig in diesem Raum. Vergiss nicht, den Schalter wieder auszuschalten!

> **Hinweis:** Force Comfort und Force Eco schließen sich gegenseitig aus – wird einer aktiviert, schaltet der andere automatisch ab.

---

## 5. Typische Anwendungsfälle

### Szenario 1: Familien-Alltag

| Raum | Zeitplan | Besonderheit |
|---|---|---|
| Wohnzimmer | 06:00–22:00 Comfort | Fenstersensor, Proximity |
| Schlafzimmer | 21:00–07:00 Comfort | 18 °C Comfort (kühler schlafen) |
| Bad | 06:00–08:00 + 19:00–22:00 | Höhere Comfort-Temp (23 °C) |
| Kinderzimmer | 07:00–20:00 | Frostschutz immer an |

### Szenario 2: Homeoffice mit Kalender

- **Büro/Schlafzimmer:** Gleicher Event ("Homeoffice"), alle Räume heizen parallel.

### Szenario 3: Urlaub

1. Alle Personen verlassen das Haus
2. Tempix senkt die Räume in den **Comfort-Phasen** ihres Zeitplans um `away_offset` — in den Eco-Phasen gilt ohnehin schon die Eco-Temperatur (siehe „Personen & Geräte")
3. Frostschutz bleibt aktiv
4. Proximity: Wenn jemand auf dem Heimweg ist, fängt Tempix an vorzuheizen

> [!TIP]
> Für eine mehrtägige Abwesenheit ist **Urlaub Abwesend** das passendere Werkzeug: er setzt einen festen Sollwert über den kompletten Zeitplan hinweg, statt nur die Comfort-Phasen abzusenken.

---

### 🖥️ Dashboard & Anzeige

**Zeitspannen-Anzeige:**
- **Ganztägig:** Ganztätige Termine im Kalender werden auf deinem Dashboard klar als "Ganztägig" beschriftet.
- **Helper-Modus:** Zeigt die aktive Comfort-Zeitspanne an (z. B. `16:30 - 21:00`). In der Eco-Phase siehst du die aktuelle Eco-Zeitspanne (z. B. `21:00 - 16:30`).
- **Kalender-Modus:** Zeigt den Event-Titel und die Zeitspanne an (z. B. `Kalender (nächster): Wohnzimmer 16:30 - 21:00`).

**Intelligente Priorisierung:**
Sollten sich Termine überschneiden (z. B. ein geplanter Urlaub und dein normaler Zeitplan), zeigt Tempix immer das **aktuell aktive** Event an. Ein "Urlaub" morgen verdrängt also nicht deine heutige Anzeige.

Der Sensor **Aktiver Zeitplan** zeigt dir im Helper-Modus `Helper: Zeitplan Küche` (aktiv) oder `Helper (nächster): Zeitplan Küche` (nächster Slot). Im Kalender-Modus erscheint `Kalender: Wohnzimmer` bzw. `Kalender (nächster): Wohnzimmer`.


---

## 6. Häufige Probleme & Lösungen

### ❓ Die Heizung reagiert nicht auf meine Änderungen

**Lösung:** Warte 2-5 Sekunden. Tempix fasst schnell aufeinanderfolgende Änderungen zusammen und rechnet erst nach einer kurzen Ruhezeit neu (`action_delay`, Standard 2 s). Bei FRITZ!DECT kommt 1–3 s Funklatenz dazu. Du kannst auch den Service `tempix.trigger_update` aufrufen.

---

### ❓ Die Temperatur springt ständig

**Lösung:** Die Kalibrierung ist zu aggressiv. Beide Stellschrauben sitzen in der Konfiguration
unter **2/6 Klima & Zeitplan** (nicht im Dashboard): **Grenzwert (Generisch)** senken
(`generic_calibration_limit`, Standard 5 °C) oder **Kalibrierungs-Delta** erhöhen
(`calibration_delta`, Standard 0.5 °C). Alternativ die Kalibrierung ganz auf „Aus" setzen.

---

### ❓ Das Fenster-Öffnen wird nicht erkannt

**Lösung:** Prüfe ob der Fenstersensor in der Integration-Konfiguration hinterlegt ist. Gehe zu **Einstellungen → Geräte & Dienste → Tempix → Konfigurieren → 1/6 Geräte & Sensoren**.

---

### ❓ Der Kalender-Event wird nicht erkannt

**Lösung:**

1. Prüfe ob das Event-Stichwort korrekt in der Konfiguration steht
2. Stichwörter sind **nicht** case-sensitive: "Homeoffice" = "homeoffice"
3. Mehrere Stichwörter mit Komma trennen: `homeoffice, büro, meeting`

---

### ❓ Der Status zeigt "Idle" statt "Comfort"

**Lösung:** „Idle" erscheint nur, solange Tempix noch keinen Zustand berechnet hat (z. B. kurz nach dem Start). Ist die Automatisierung ausgeschaltet, zeigt der Status stattdessen `Automation Off` (siehe nächster Abschnitt).

---

### ❓ Der Status zeigt "Inactive" – was kann die Ursache sein?

Es gibt mehrere Gründe warum Tempix `Inactive` anzeigt:

| Ursache | Was tun? |
|---------|----------|
| 🌡️ **Außentemperatur über Schwelle** | Status zeigt `Outside > Threshold` (beim Kühlen `Outside < Threshold`). Korrekt — Tempix heizt nicht wenn es draußen warm genug ist. Schwelle unter **1/6 Geräte & Sensoren → Außentemperatur → Klimaschwelle (Außen)** prüfen falls gewünscht. |
| 🏖️ **Saison Modus** ausgeschaltet | Status zeigt `Season Off`. Den Saison-Schalter (`input_boolean.heizperiode`) einschalten oder Saison Modus in der Konfiguration deaktivieren. |
| 🤖 **Automation deaktiviert** | Status zeigt `Automation Off`. **Automatisierung Aktiv** ist ausgeschaltet — Switch einschalten. |
| ❄️ **Kein Sensor / unavailable** | Hat Tempix gar keine Raumtemperatur (weder Raumsensor noch Thermostat liefern einen Wert), zeigt der Status `Paused` statt `Inactive`. Fällt nur ein Sensor aus, bleibt das normale Label stehen. Hat Tempix dann nur noch einen Ersatzwert, erscheint nach 15 Minuten die Ersatzwert-Meldung (siehe Hinweis unten); bei mehreren Raumsensoren rechnet Tempix einfach mit den erreichbaren weiter. Entity-Verfügbarkeit in HA prüfen. |

> [!TIP]
> Den genauen Grund siehst du im Diagnose-Export unter **Einstellungen → Geräte → Tempix → Diagnose herunterladen** → `engine_state.automation_active`, `season_mode`, `outside_temp_ok`.

> [!NOTE]
> Du musst dafür nicht aktiv nachschauen: Bleibt ein Raum länger als 15 Minuten durchgängig pausiert, erscheint automatisch eine Meldung unter **Einstellungen → System → Reparaturen** mit Grund und Zeitpunkt. Sie verschwindet von selbst, sobald der auslösende Sensor wieder erreichbar ist.

> [!NOTE]
> Fällt ein Sensor aus, aber Tempix hat noch einen Ersatzwert (z. B. den eingebauten Fühler des Thermostats oder den letzten bekannten Fensterzustand), erscheint nach 15 Minuten die Meldung **„läuft mit einem Ersatzwert“**. Je nach Ursache regelt Tempix weiter oder hält die Thermostate auf ihrem letzten Sollwert an — Letzteres, wenn **alle** Raumsensoren ausgefallen sind (nur der Frostschutz greift dann noch ein). Prüfe die Sensoren des Raums (einen Fenstersensor nennt die Meldung beim Namen); die Meldung verschwindet von selbst, sobald der Eingang wieder gültig ist.

> [!NOTE]
> Ist dein Thermostat oder Klimagerät selbst nicht mehr erreichbar (Strom, Batterie oder Funk weg), kann Tempix es nicht steuern – es könnte in seinem letzten Zustand festhängen (z. B. weiterheizen). Nach 15 Minuten erscheint dazu eine Meldung unter **Einstellungen → System → Reparaturen**. Prüfe dann Stromversorgung, Batterie und Funkverbindung des Geräts. Auch diese Meldung verschwindet von selbst, sobald das Gerät wieder erreichbar ist. Steht der Raum im **manuellen Modus**, kommt die Meldung als Warnung: Tempix steuert dort bewusst nicht, aber deine Eingaben auf der Tempix-Karte erreichen das Gerät nicht – die Karte zeigt sie trotzdem an. Ist das Gerät absichtlich aus (z. B. die Klimaanlage über den Winter), klicke in der Meldung auf **Ignorieren**; sie bleibt dann auch über Neustarts ausgeblendet, bis das Gerät wieder erreichbar war.

> [!NOTE]
> Hast du einen Kalender, eine Person oder einen Sensor in
> Home Assistant **gelöscht oder umbenannt**, bleibt der Eintrag in den Einstellungen des
> Raums stehen und zeigt ins Leere – ohne Fehlermeldung. Die Funktion dahinter fällt still aus (fehlt z. B. ein Kalender, springt die Abwesenheitserkennung nicht mehr an). Nach 15 Minuten erscheint dazu eine Meldung unter **Einstellungen → System → Reparaturen**,
> die dir die betroffenen Einträge nennt. **Diese eine Meldung verschwindet nicht von
> selbst** – eine gelöschte Entity kommt nicht zurück. Entferne den Verweis in
> den Einstellungen des Raums, oder stelle die Entity wieder her.

---

### ❓ Ich will die Heizung kurz manuell steuern

**Lösung:** Aktiviere **Manueller Modus** (✋). Tempix greift dann gar nicht mehr ein und du kannst den Thermostat frei einstellen. Wenn du fertig bist, schalte den Switch wieder aus – Tempix übernimmt sofort wieder.

Du musst dafür nicht auf die einzelnen Heizkörper-Thermostate ausweichen: Solange die Pause aktiv ist, leitet das Tempix-Thermostat (`climate.<name>`) selbst jede Temperatur-/Modus-Änderung direkt an alle angeschlossenen Geräte weiter (Passthrough-Modus) — du kannst also einfach die gewohnte Tempix-Karte im Dashboard weiterbenutzen.

> [!NOTE]
> **In der Pause zeigt die Karte den echten Gerätezustand:** Modus und Zieltemperatur des Geräts –
> ein laufendes Gerät bleibt nicht unbemerkt, auch nicht nach einem Neustart von Home Assistant.
>
> Deine Eingabe erscheint sofort auf der Karte, wartet dahinter aber
> auf die Bestätigung der Geräte — bei einem nicht erreichbaren Gerät im Raum kann das bis
> zu 10 Sekunden dauern. Es gibt keinen automatischen zweiten Versuch; einfach nochmal einstellen,
> wenn es nicht angekommen ist.

---

### ❓ Kann ich zwischendurch einfach auf "Nur Lüfter" oder "Entfeuchten" stellen?

**Ja, direkt auf der Tempix-Karte.** Sie zeigt alle Modi an, die deine Geräte
im Raum tatsächlich können — bei einer Klimaanlage also auch *Entfeuchten* (`dry`) und
*Nur Lüfter* (`fan_only`).

Das verhält sich wie jeder andere Griff ans Thermostat: Tempix hält deinen Modus bis zur
nächsten Zeitplan-Phase (spätestens nach 24 Stunden) und übernimmt danach wieder von selbst.
Vorzeitig beenden kannst du es über den Schalter **Temporäre Manuelle Steuerung**.

> [!NOTE]
> **In diesen beiden Modi wird keine Zieltemperatur gesetzt** — Geräte haben dort keine. Der
> Temperaturregler auf der Karte ist deshalb ohne Funktion, solange *Entfeuchten* oder
> *Nur Lüfter* aktiv ist. Das ist kein Fehler.

> [!TIP]
> **Nichts Neues zu sehen?** Dann melden deine Thermostate diese Modi nicht — bei
> Heizkörper-Thermostaten ist das der Normalfall, sie können schlicht nur heizen.

**Was (noch) nicht geht:** `dry`/`fan_only` als festen *Zeitplan*-Modus hinterlegen, also
„jeden Nachmittag entfeuchten". Dafür stehen in der Konfiguration weiterhin nur Heizen, Kühlen,
Heizen / Kühlen (falls die Thermostate es können), Auto und Aus zur Wahl.

---

### ❓ "Manual Mode" und "Temporary Manual Override" – was ist der Unterschied?

Die Namen ähneln sich, gemeint ist aber fast das Gegenteil. Merksatz: **das eine schaltet Tempix
ab, das andere ist Tempix, das dir zuhört.**

| | ✋ **Manual Mode** (Pause) | ⏱️ **Temporary Manual Override** |
|---|---|---|
| **Was ist es?** | Ein Schalter, den *du* umlegst | Passiert von selbst, wenn du am Thermostat drehst |
| **Was macht Tempix?** | **Gar nichts mehr** – keine Temperatur, kein Modus, keine Kalibrierung, kein Frostschutz | Regelt normal weiter, respektiert aber deinen Wert |
| **Wie lange?** | Bis *du* den Schalter wieder ausschaltest | Bis zur nächsten Zeitplan-Phase, spätestens nach 24 Stunden |
| **Schutzfunktionen?** | Aus. Auch der Frostschutz | Aktiv. Fenster-Erkennung und Frostschutz übersteuern deinen Wert |
| **Status-Sensor zeigt** | `Manual Mode` | `Temporary Manual Override` |
| **Entity** | `switch.<name>_manueller_modus` | `switch.<name>_temporare_manuelle_steuerung` |

**Im Alltag heißt das:**

- Du drehst am Thermostat oder an der Tempix-Karte → **Temporary** Override. Das ist der
  Normalfall und meist genau das, was du willst: heute Abend wärmer, morgen früh wieder nach
  Plan. Beenden kannst du ihn vorzeitig, indem du den Temporary-Switch ausschaltest.
- Du willst Tempix für längere Zeit komplett aus dem Weg haben (Handwerker, Umbau, Fehlersuche)
  → **Pause**. Nicht vergessen, sie wieder auszuschalten — solange sie läuft, greift auch der
  Frostschutz nicht.

> [!TIP]
> Wenn ein Raum unerwartet auf `Temporary Manual Override` steht: Der Diagnose-Export zeigt
> unter `decision_logic.override`, *woher* der Eingriff kam (`ui` = Tempix-Karte,
> `physical` = Gerät oder eine andere Automation) und *seit wann*.

---

### ❓ Ich habe den Thermostat während eines WLAN-Ausfalls manuell verstellt – Tempix hat es zurückgesetzt

**Das ist kein Fehler.** Tempix erkennt manuelle Änderungen nur über State-Change-Events aus der HA State Machine. Während eines Netzwerkausfalls ist der TRV `unavailable` – Events kommen nicht an. Nach dem Reconnect setzt Tempix die laut aktivem Plan korrekte Temperatur.

**Workarounds:**
- Nutze **Manueller Modus** (✋), um Tempix komplett zu pausieren – volle manuelle Kontrolle.
- Nutze **Comfort-Temperatur erzwingen** (🔥), um die Automatik dauerhaft zu überschreiben – dieser Schalter überlebt den Reconnect.
- Oder passe deinen Zeitplan kurz an, falls du eine andere Temperatur dauerhaft willst.

---

### ❓ Die Heizung läuft in der warmen Jahreszeit

**Lösung:** Stelle sicher, dass ein Helfer unter **Saison Modus** (`input_boolean.heizperiode`) konfiguriert und auf ON gesetzt ist, oder dass der **Außentemperatur-Grenzwert** richtig eingestellt ist.

---

### 🛠️ Diagnose & Hilfe

Wenn etwas nicht so funktioniert wie geplant und du Hilfe im Forum oder auf GitHub suchst, sind **Diagnose-Daten** extrem hilfreich.

**So erstellst du einen Diagnose-Export:**

1. Gehe zu **Einstellungen → Geräte & Dienste**.

2. Suche nach der **Tempix** Karte.

3. Klicke auf die drei Punkte (`⋮`) und wähle **Diagnose herunterladen**.

Die heruntergeladene Datei enthält alle technischen Details deiner Konfiguration und den aktuellen Zustand deiner Heizung (anonymisiert), was die Fehlersuche massiv beschleunigt.

---

## 7. Best Practices

### ✅ Empfehlungen

| Empfehlung | Warum |
|---|---|
| **Externen Temperatursensor nutzen** | TRV-Sensoren messen am Heizkörper – zu warm! |
| **Kalibrierung auf "Generisch" setzen** | Funktioniert mit jedem TRV-Typ |
| **Frostschutz aktivieren** | Schutz vor Rohrbruch |
| **Proximity nutzen** | Vorheizen auf dem Heimweg = warme Wohnung |
| **Aggressiver Modus** | Schnelles Aufheizen bei großen Temperatur-Differenzen |
| **Abwesenheits-Offset: 2–3 °C** | Guter Kompromiss aus Komfort und Sparen |
| **Party-Modus per Service** | Mit Timer-Funktion vergisst du nicht abzuschalten |
| **Kalender statt Schedule** | Flexibler für unregelmäßige Tage |

### ❌ Fehler vermeiden

| Fehler | Konsequenz |
|---|---|
| Kein Saison Modus konfiguriert | Heizung läuft auch im Sommer |
| Window Open Temp = 0 ohne Sensor | TRV geht komplett aus |
| Zu viele Scheduler | Verwirrend, max. 3 empfohlen |
| Smart Pre-Conditioning ohne Sensor | Kann nicht lernen → Fallback |

---

## 8. Profi-Funktionen: JSON-Anpassungen (Adjustments)

Für Experten bietet Tempix die Möglichkeit, die Heizlogik über **JSON-Anpassungs-Mappings** extrem fein zu steuern. Damit lassen sich zeit-, zeitplan- oder kalenderbasierte Ausnahmen definieren.

### 🎛️ Parameter-Referenz

| Feld | Icon | Beschreibung | Beispiel |
|:--- |:---:|:--- |:--- |
| **time** | 🕔 | **Pflicht.** Uhrzeit, ab der die Regel gilt. | `"08:00"` |
| **days** | 📅 | Liste der Wochentage für diese Regel. | `["Sat", "Sun"]` |
| **scheduler**| 🗓️ | Regel gilt nur für diesen Zeitplan-Namen. | `"Urlaub"` |
| **comfort** | 🛋️ | Überschreibt die Comfort-Temperatur. Statt einer Zahl ist auch eine Entity-ID möglich (z.B. `input_number.morgen_temp`) — der Wert wird dann live aus dem Helper gelesen. | `20.5` oder `"input_number.morgen_temp"` |
| **eco** | 🌱 | Überschreibt die Eco-Temperatur. Akzeptiert ebenfalls eine Entity-ID. | `17.0` oder `"input_number.abend_temp"` |
| **mode** | 🔘 | Erzwingt einen Modus (`comfort`, `eco`, `off`, `auto`). | `"eco"` |
| **calibration**| 🧭 | Schaltet die Kalibrierung ein oder aus (`on`, `off`). | `"off"` |

> [!TIP]
> Es gilt die Regel mit der spätesten Uhrzeit, die schon erreicht ist. Die Reihenfolge in der Liste spielt keine Rolle – nur bei gleicher Uhrzeit gewinnt die obere.

### 📝 Anwendungs-Beispiel

Stell dir vor, du möchtest am Wochenende im Zeitplan "Urlaub" die Temperatur absenken und die Kalibrierung nachts ausschalten:

```json
[
  {
    "time": "08:00",
    "comfort": 20.5,
    "calibration": "off"
  },
  {
    "time": "20:00",
    "days": ["Sat", "Sun"],
    "scheduler": "Urlaub",
    "mode": "off"
  }
]
```

---

## 9. Dashboard-Tipps

### Nützliche Entities für Dashboards

| Entity | Darstellung | Nutzen |
|---|---|---|
| `climate.<name>` | Thermostat-Karte | Hauptsteuerung |
| `sensor.<name>_status` | Markdown | Alle Details als Attribute |
| `sensor.<name>_ziel_temperatur` | Gauge | Solltemperatur auf einen Blick |
| `binary_sensor.<name>_fenster_offen` | Chip | Fenster-Status |
| `binary_sensor.<name>_anwesenheit_personen` | Chip | Anwesenheit |
| `switch.<name>_party_modus` | Button | Schnellzugriff Party |
| `switch.<name>_manueller_modus` | Button | Manueller Modus (Pause) |
| `sensor.<name>_aktiver_zeitplan` | Text | Welcher Plan aktiv ist |

### Beispiel: Minimal-Dashboard

> [!NOTE]
> **Die Entity-IDs hängen von deiner HA-Sprache ab** — Home Assistant bildet sie aus dem
> übersetzten Anzeigenamen und legt sie bei der Einrichtung einmalig fest. Das Beispiel zeigt
> eine deutsche Neuinstallation. Deine eigenen IDs listet dir dieser Einzeiler auf
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
      - switch.wohnzimmer_heizung_gaste_modus
      - switch.wohnzimmer_heizung_manueller_modus
      - switch.wohnzimmer_heizung_comfort_temperatur_erzwingen
      - switch.wohnzimmer_heizung_eco_temperatur_erzwingen
      - switch.wohnzimmer_heizung_smartes_vor_temperieren
```
