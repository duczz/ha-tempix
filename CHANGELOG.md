# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

## [1.7.0] - 2026-11-11

### 🐛 Bug Fixes

**Override, Kalender & Zeitplan**
- **Manueller Override endete bei Feiertags-/Wochentag-Umleitung (`holiday_use_day`) zu früh (bei Nachtphasen bis zu 75 Min.):** Er endet jetzt am Ende der umgeleiteten Phase (Kalender- und Helper-Modus). Änderungen an der Schedule-Definition sowie gelöschte oder geänderte Kalendertermine wirken sofort bzw. nach wenigen Sekunden statt erst beim nächsten Abruf (bis 15 Min.). Änderungen, die nur auf dem Server eines Online-Kalenders (CalDAV, URL-Abo) liegen, sieht Tempix erst nach dem nächsten Abruf.
- **Wochentag-Umleitung (`use_day`, `vacation_home`, Feiertag) nahm fremde oder falsche Termine:** Es zählen jetzt nur Termine, die die Ereignis-Stichwörter erfüllen und zum Raum passen (Ort, ersatzweise Titel). Gibt es keinen, entfällt die Umleitung (Warnung „not found") und der normale Zeitplan bzw. Helper-Zeitplan gilt. Ganztagstermine gewinnen nur noch, wenn kein zeitgebundener Termin übrig ist, und gelten als ganzer Tag Comfort. **Hinweis:** Wer sich auf den Rückgriff auf Termine anderer Räume verlassen hat, folgt am Feiertag jetzt dem normalen Zeitplan; Ort oder Titel des Zieltag-Termins müssen den Raum nennen.
- **Das Kalender-Tag `time: HH:MM - HH:MM` hatte keine Wirkung:** Der Termin liefert Comfort jetzt nur innerhalb des Fensters, auch über Mitternacht und bei per `use_day` geerbtem Tag; mit Smart Pre-Conditioning wird vor dem Fensterbeginn vorgeheizt bzw. vorgekühlt. Fensterbeginn und -ende sind Zeitplanwechsel (ein Override endet dort). **Verhaltensänderung:** Wer das Tag schon nutzt, bekommt außerhalb des Fensters Eco statt Comfort. Wirkt nur im Kalender-Modus; `24:00` ist ungültig (stattdessen `23:59`). Ein beim Update aktiver Override kann einmalig enden.
- **Smart Pre-Conditioning im Kalender-Modus:** Es heizt nicht mehr auf das *Ende* eines laufenden Termins vor, sondern nur noch auf einen Terminbeginn (**Verhaltensänderung** bei nicht verfügbarer Kalender-Entity oder überstimmtem Termin). Kühlräume, die schon kühler als Comfort sind, bekommen vor einem Termin kein Comfort mehr. Die Vorlaufzeit zählt in echter Zeit, auch in den Nächten der Zeitumstellung (vorher bis zu eine Stunde daneben).
- **Ein gelöschter Kalender schaltete die Abwesenheitserkennung eines Raums still ab:** Gelöschte Kalender werden jetzt übersprungen, die vorhandenen weiterverwendet. Ein nur vorübergehend nicht erreichbarer Kalender führt weiter zur vorsichtigen Variante (keine Abwesenheit).
- **Kalender-Raumfilter scheiterte an Umlauten in anderer Unicode-Schreibweise** (z. B. „Büro" aus Google Calendar oder macOS): Der Raum bekam nie seine Comfort-Phase, ohne Logeintrag. Behoben.
- **Ein einmal konfigurierter Feiertagskalender ließ sich in den Raum-Einstellungen nicht mehr entfernen.** Behoben.
- **Termine über Mitternacht erschienen im Sensor „Aktiver Zeitraum" als „10:00 - 10:00":** Endet ein Termin an einem anderen Tag, steht jetzt der Wochentag dabei (z. B. „Di. 10:00 - Mi. 10:00"), wie in der Termin-Ansicht von Home Assistant. Reine Anzeige.

**Zeitzonen**
- **Kalkschutz-Fahrt, Anpassungs-Blöcke (Adjustments) und das Präsenz-Komfortfenster rechneten in UTC bzw. Systemzeit statt in der Home-Assistant-Zeitzone:** In Deutschland starteten sie 1–2 Stunden zu spät bzw. zur falschen Tageszeit. Ein kaputtes oder leeres Start/Ende-Feld des Präsenzfensters warnt jetzt mit den konkreten Werten.

**Heizen, Kühlen & Schutzfunktionen**
- **Fenster, das während der Schließ-Verzögerung wieder geöffnet wurde, galt kurz als geschlossen:** Für die Öffnungs-Verzögerung (Standard 1 Min.) heizte bzw. kühlte der Raum regulär, bis das Öffnen bestätigt war. Das Fenster gilt jetzt lückenlos als offen. Das gilt auch, wenn in einem Raum mit mehreren Fenstersensoren ein anderer Sensor kurz nach dem Schließen öffnet. Eine Öffnung, die die Öffnungs-Verzögerung nie überlebt hat (z. B. Kontaktprellen), löst beim Schließen keine Schließ-Verzögerung mehr aus (Ausnahme: Wird das Fenster innerhalb einer laufenden Schließ-Verzögerung wieder geöffnet und erneut geschlossen, startet die Verzögerung neu). Ohne eingestellte Schließ-Verzögerung ändert sich nichts.
- **Klimaanlage ging nachts fälschlich aus:** Ein kurzer Verfügbarkeits-Aussetzer eines Fenstersensors (z. B. Router-Neustart) wurde als „Fenster geschlossen" gewertet. Behoben.
- **Sonnenschein-Offset war beim Kühlen invertiert** (Statusanzeige ebenfalls). Er senkt jetzt in beiden Richtungen die Zieltemperatur.
- **Frostschutz hielt eine Klimaanlage bei offenem Fenster aktiv** (`window_open_temp` = 0): Frostschutz gilt jetzt nur beim Heizen.
- **Ausgefallener Außentemperatur-Sensor schaltete die gesamte Automatik ab, Frostschutz eingeschlossen:** Der Fallback ist jetzt standardmäßig „Automatik bleibt aktiv". Wer den Wert im Experten-Bereich selbst gesetzt hat, ist nicht betroffen.
- **„Comfort erzwingen":** Ein offenes Fenster gewinnt jetzt auch dagegen (Ausnahme: Kalkschutz-Fahrt). Bei Klimaanlagen ging bisher der Höchst- statt der Mindestwert ans Gerät (geringstmögliche Kühlung); jetzt geht beim Kühlen der Mindestwert raus. Dasselbe gilt für die Kalkschutz-Fahrt im Kühlbetrieb.
- **Kühlräume liefen in Sonderfällen auf der stärksten statt der schwächsten Stufe** („TRV bei Zielerreichung eher auf Minimum als auf Aus", Klimaanlagen ohne Aus-Modus, offenes Fenster mit `window_open_temp` über 0). Behoben.
- **Vorheizen/Vorkühlen:** Die gelernte Heizrate und die Vorlaufzeit reagierten bei Klimaanlagen falsch herum auf die Außentemperatur (für Kühlräume jetzt neutral); bei Kälte war der Vorlauf an einer Stelle zu kurz; ein von der Anwesenheitsprüfung abgelehntes Vorheizen konnte dennoch auf Comfort heizen.
- **Die gelernte Aufheizrate wurde bei jeder Bedienung verworfen** und fiel auf 1,0 °C/h zurück. Sie überlebt jetzt jede Einstellungsänderung; ein von Hand gesetzter Wert gewinnt und wird sofort gespeichert.
- **Die Außentemperatur-Hysterese ging bei jeder Bedienung verloren:** Ein Klick auf Party konnte einen Raum bei grenzwertiger Außentemperatur abschalten. Behoben.
- **Hysterese 0 °C** ließ das Gerät jeden Zyklus ein- und ausschalten: Minimum jetzt 0,1 °C, auch für bestehende Konfigurationen.
- **Ein ausgefallener von mehreren Raumsensoren legte den Raum still:** Es zählt jetzt der Median der gesunden Sensoren. Unplausible Messwerte (`NaN`, `Infinity`) werden abgefangen.
- **Party-Temperatur wich um bis zu 5 °C von der Anzeige ab**, wenn sie nicht gesetzt war (die Regelung nahm Comfort statt 18 °C). Behoben.
- **Nicht konfigurierte Zahlenwerte** (Urlaubs-, Party-, Sonnenschein-Temperatur) fielen nach einem Neustart auf den Standard zurück. Behoben.

**Ventil, Kalkschutz & Urlaub**
- **Ventilsteuerung lief, obwohl sie ausgeschaltet war** (betraf nur Setups mit Ventil-Entity `number.*`; Alt-Werte werden automatisch umgesetzt). Außerdem wurde das Ventil bei „Gerät aus" aufgedreht; jetzt wird es geschlossen.
- **Kalkschutz bewegte das Ventil nicht,** wenn das Gerät schon aus war; **Kalkschutz und Urlaub liefen ins Leere,** wenn der Eco-Modus auf „Aus" stand. Beides behoben.
- **Sonoff TRVZB (Zigbee2MQTT): Die Ventilsteuerung konnte den falschen Wert beschreiben.** Das Standard-Stichwort `valve` trifft dort `valve_opening_degree` und `valve_closing_degree`; je nach Reihenfolge ging die Ventilöffnung an den Schließgrad. Ein Schließgrad wird jetzt nie beschrieben (außer das Stichwort nennt ihn ausdrücklich), bei mehreren Treffern gewinnt der Öffnungsgrad, auch nach dem Umbenennen der Entities. Bleibt es mehrdeutig, schreibt Tempix nichts und warnt einmal im Log.
- **Die Ventilöffnung konnte über der eingestellten Maximalöffnung liegen,** wenn die Maximalöffnung kein Vielfaches der Schrittweite war (z. B. Maximum 97 %, Schritt 10 → 100 %). Sie bleibt jetzt höchstens beim Maximum.

**Geräte & Verbindung**
- **Ein Verbindungsaussetzer eines Thermostats konnte den Raum stundenlang lahmlegen:** Die Rückkehr aus *nicht verfügbar* (z. B. durch eine andere Integration wie FRITZ!Box) gilt jetzt als Wiederanmeldung, nicht als Handgriff; der Sollwert wird sofort wieder durchgesetzt. *Preis:* Eine Drehung am Thermostat *während* des Ausfalls wird dabei überschrieben.
- **Ein fehlgeschlagener Start der Integration räumt jetzt seine Listener und Timer wieder auf,** bevor Home Assistant es erneut versucht; vorher konnten sie liegen bleiben.
- **Nach jedem Lüften blieb der Raum auf dem Wert des Thermostats statt auf dem Zeitplan** (bestätigt: FRITZ!DECT Comet stellt beim Einschalten seinen eigenen Sollwert wieder her): Diese Rückmeldung gilt jetzt als Zwischenschritt der Befehlsbestätigung. *Preis:* Eine Drehung in dieser kurzen Spanne wird überschrieben.
- **Falsche manuelle Eingriffe vermieden:** Eine einzelne Handdrehung wurde als Geräte-Echo abgetan, solange ein Kommando noch aussteht (Vergleich jetzt exakt statt mit 0,5-°C-Toleranz); ein Reload oder das Entladen der Integration konnte einen Eingriff erzeugen, den niemand ausgelöst hatte.
- **Ein Kartenbefehl während der Pause konnte spurlos verschwinden:** Der Passthrough wartet jetzt bis zu 10 Sekunden auf die Bestätigung jedes Geräts und meldet nicht antwortende Geräte im Log.
- **Während der Pause zeigte die Karte die Rechnung statt der Wirklichkeit:** Sie spiegelt jetzt Modus und Zieltemperatur des Geräts.

**Dienste, Einstellungen & Dialog**
- **Der Dialog nahm Adjustments an, die nie wirkten** (z. B. `"time": "9:00"` statt `"09:00"`, Eintrag ohne `time`, unbekannter Modus, Tag `sat` statt `Sat`); erst danach meldete „Reparaturen" sie. Der Dialog lehnt jetzt genau das ab, was auch die Reparatur-Prüfung beanstandet, und ein kaputtes Adjustments-JSON mit Fehlermeldung (vorher still gespeichert).
- **Dienste (`trigger_update`, `set_party_mode`, `set_guest_mode`):** Eine unbekannte `config_entry_id` wurde still ignoriert (jetzt Fehlermeldung; ohne ID wirken sie weiter auf alle Räume); fehlende Pflichtfelder geben eine verständliche Meldung; die Dienste werden einmal statt pro Raum registriert; ein hausweiter Aufruf brach ab, wenn währenddessen ein Raum neu geladen wurde.
- **Der Einstellungs-Dialog überschrieb Änderungen, die während er offen war** (z. B. Komfort erzwingen, ablaufender Party-Timer, gelernte Rate): Es gewinnen nur noch die Felder, die im Dialog tatsächlich geändert wurden.
- **Die Auswahl „HVAC-Modus Comfort/Eco" bot Modi an, die das Thermostat nicht kann** (z. B. „Auto" auf einem FRITZ!DECT Comet → Gerät aus, ohne Warnung): Angeboten wird jetzt, was alle Thermostate des Raums können; ein gespeicherter Wert bleibt immer wählbar. **Hinweis:** In Räumen mit gemischten Geräten zeigt die Auswahl nur die gemeinsamen Modi; `select.select_option` lehnt nicht angebotene Modi ab.
- **Ein erneutes Öffnen der Einstellungen konnte den HVAC-Modus still umschreiben,** wenn das Thermostat den Modus gerade nicht meldete. Behoben.
- **Räume, die vor 1.6.5 angelegt wurden, konnten beim Speichern der Einstellungen ihr Abwesenheits-Verhalten verlieren:** Der Dialog zeigte „Ignorieren“ statt des tatsächlich aktiven „Offset“ und schrieb es beim Speichern fest. Er zeigt jetzt den Wert, mit dem der Raum wirklich läuft.
- **Vier Einstellungen wirkten erst nach einem Neustart** (Feiertagskalender, Zeitplan-Quelle, Kalender-Abfrageintervall, Entity-Referenzen für Gäste-Modus, Anwesenheits-Zeitplan, Wetter und Adjustments): Sie laden den Eintrag jetzt sofort neu.
- **Nach dem Umbenennen eines Raums** nannten Log-Einträge und Reparatur-Hinweise den alten Namen; sie folgen jetzt sofort. (Der Gerätename auf der Geräteseite folgt weiter erst beim nächsten Neuladen — Home-Assistant-Verhalten; dort das *Gerät* umbenennen.)

**Party, Gäste & Karte**
- **Party-/Gäste-Modus mit Zeitbegrenzung blieb nach einem Neustart ewig an,** und ein alter Timer überstimmte später den Schalter. Beides behoben.
- **Die Karte erlaubte Zieltemperaturen bis 32 °C, gesendet wurden höchstens 30 °C:** Die Obergrenze liegt jetzt überall bei 30 °C.
- **Bei ausgeschaltetem „Temporärer manueller Modus" verwarfen Karte (Temperatur- und Moduswechsel) und Schalter Eingaben still:** Beide melden jetzt, dass die Funktion in den Optionen aktiviert werden muss. In der Pause gehen Eingaben weiter direkt ans Gerät.

**Anzeige & Diagnose**
- **Nach Quellenverlust zeigten Sensoren und Karte den Wert vom letzten Neustart statt „unbekannt":** Der gespeicherte Wert überbrückt jetzt nur noch die Zeit bis zum ersten gültigen Wert nach dem Start.

**Kompatibilität**
- **Deprecation-Warnungen unter Home Assistant 2026.11 (`current_temperature`, `target_temperature`, `_attr_temperature_unit`):** Die Klima-Entity nutzt jetzt die neuen `native_*`-Properties und bleibt über Aliase mit älteren Home-Assistant-Versionen kompatibel. Keine Verhaltensänderung, Werte und Einheit bleiben gleich. Die Mindestversion in `hacs.json` ändert sich nicht.

### ✨ New Features
- **Reparatur-Meldungen, wenn ein Raum ungeregelt bleibt** (*Einstellungen → System → Reparaturen*, mit Grund, Zeitpunkt und Geräten): Raum dauerhaft pausiert, technischer Dauerfehler, Gerät nicht erreichbar (im manuellen Modus als Warnung, denn die Karte zeigt dort trotzdem den eingestellten Zustand), Kalibrierung steht still (2 h), Raum läuft auf Ersatzwert, eingetragene Entity existiert nicht mehr. Außer der letzten verschwinden sie von selbst, sobald der Raum wieder normal läuft.
- **Hinweise zu widersprüchlichen Einstellungen unter „Reparaturen"** (z. B. Comfort/Eco in falscher Richtung, unbrauchbare Adjustments, Feiertagskalender ohne Ersatz-Wochentag, Smart-Vorheizen mit 0 Vorlauf). Tempix startet weiter mit jeder Konfiguration.
- **Frostschutz schaltet die Heizung selbst ein, wenn die Automatik aus ist** (Saison-Schalter, Master-Schalter, ausgefallener Außensensor) und der Raum unter die Frostschutz-Temperatur fällt. Ein offenes Fenster gewinnt weiterhin.
- **Entfeuchten (`dry`) und Nur Lüfter (`fan_only`)** sind direkt auf der Tempix-Karte wählbar (ohne Zieltemperatur; wie jeder manuelle Eingriff bis zur nächsten Phase, maximal 24 Stunden). Als dauerhafter *Zeitplan*-Modus noch nicht möglich.
- **Neuer Schalter „Urlaub Zuhause":** Löst die Wochentag-Umleitung (`holiday_use_day`) manuell und pro Raum aus, ohne Feiertagskalender. Optional mit eigenem Wochentag über `vacation_home_use_day`. Nicht zu verwechseln mit „Urlaub Abwesend".
- **Party- und Gäste-Modus lassen sich für einen einzelnen Raum schalten:** `tempix.set_party_mode` und `tempix.set_guest_mode` nehmen optional einen Raum entgegen; ohne Angabe bleibt es hausweit.
- **`proximity_duration` funktioniert jetzt:** `arrived`/`towards` zählen erst nach der konfigurierten Dauer (dämpft GPS-Flackern); Standard 0.
- **Attribut `source_climates`** an der Tempix-Karte listet die gesteuerten Original-Geräte.
- **Mindestöffnung für die Ventilsteuerung (`valve_min`):** Viele Ventile bleiben bis zu einer bestimmten Öffnung (z. B. 18 %) physisch zu. Kleine Öffnungen wie 10 % hatten dort keine Wirkung. Unter *Ventilsteuerung* lässt sich jetzt der Schließpunkt eintragen. Tempix verteilt dann jede Öffnung über 0 auf den Bereich von diesem Wert bis zur maximalen Öffnung. Schon die kleinste Öffnung lässt also Wasser durch. Zu (0 %) bleibt zu, auch bei „Aus". Standard 0 = keine Mindestöffnung, bestehende Einstellungen ändern sich nicht. Liegt die Mindestöffnung nicht unter der Maximalöffnung, meldet Tempix einen Reparatur-Hinweis.

### 🎨 Improvements
- **Der Pause-Schalter heißt jetzt „Manual Mode" („Manueller Modus"):** Der Key wechselt von `manual_override` auf `manual_mode` (Attribut, Status-Text, `heating_state`). Eine laufende Pause und die `unique_id` werden automatisch übernommen; die Entity-ID bestehender Installationen bleibt `switch.<name>_manual_override`. **Zu prüfen:** Dashboards, Templates und Automationen, die `manual_override` lesen.
- **Der Schalter „Urlaubs-Modus" heißt jetzt „Urlaub Abwesend"** (nur der Anzeigename).
- **Alle Tempix-Dateien liegen jetzt in `.storage/tempix/`** (vorher sieben Dateien pro Raum flach). Der Umzug passiert beim ersten Start automatisch; die alten Dateien bleiben bewusst bestehen, eine spätere Version räumt sie selbst auf (nicht von Hand löschen). Bei Rückkehr auf eine Version vor 1.7.0 gehen ein laufender Override und Party-Timer verloren, die gelernte Rate fällt auf einen älteren Wert zurück.
- **Der Status nennt jetzt den Grund, warum nicht geregelt wird:** `Season Off`, `Automation Off`, `Outside > Threshold` (beim Kühlen `Outside < Threshold`), bei mehreren Gründen alle zusammen.
- **Ein Thermostat lässt sich beim Einrichten nicht mehr in einem zweiten Raum anlegen** (der Dialog zeigt einen Fehler); bestehende Räume bleiben unverändert.
- **Ventilsteuerung ist wieder ein Auswahlfeld** mit *Aus*, *Linear*, *Pessimistisch* und *Optimistisch*.
- **Kühlräume:** „Lüftungs-Temperatur" und „Ruhetemperatur" gehen jetzt bis 30 °C (vorher 20 °C).
- **Alle Temperatur-Felder im Dialog sind Slider;** „Anpassungen (JSON)" sitzt jetzt in Schritt 2 („Klima & Zeitplan").
- **Einheitliche Temperatur-Obergrenzen:** Comfort-/Eco-/Party-Entities gehen bis 30 °C (vorher 35 °C).
- **Diagnose-Export:** Er nennt jetzt den Grund eines Raum-Stillstands (`coordinator_state.circuit_breaker`), Herkunft, Zeitpunkt und Werte eines Overrides, die Raumfilter-Markierung je Termin und die einzelnen Kanäle des Annäherungs-Geräts samt Verdikt. Namen, Orte und Mailadressen werden nicht mehr exportiert, auch nicht im Umleitungs-Event, das vorher Titel, Ort, Beschreibung und Kalender-ID im Klartext enthielt (Kalender als `calendar_0`, …, Zonen als `<zone>`, unerwartete Kanal-Werte als `<masked>`); Kalender-Start- und -Endzeiten bleiben enthalten — einen *privaten* Kalender vor dem Anhängen an ein öffentliches Issue durchsehen.
- **Status-Text bei Abwesenheit:** Der Status-Sensor zeigt bei aktivem Away jetzt `Away (🚶 -2.0°C)` bzw. `Away (🚶 Eco)` statt `Comfort (🚶 …)`. Sonnenschein-Offset und Kalender-Temperatur stehen weiter in der Klammer. Reine Anzeige, die Zieltemperatur ändert sich nicht. Wer den Status-Text in Automationen oder Dashboards auswertet, muss `Comfort (` durch `Away` ersetzen.

### 🗑️ Removed
- **Frostschutz-Einstellung „Frostschutz nach" (`frost_protection_duration`) entfernt:** Sie gehörte zu einer Abwesenheitsregel (Absenken nach längerer Abwesenheit), nicht zu einem Schutz. Der Frostschutz arbeitet jetzt als **Untergrenze** des Sollwerts, unabhängig von Anwesenheit, Phase und Fenster (nur beim Heizen). Zum Absenken bei Abwesenheit dient „Urlaub Abwesend" oder das Away-Verhalten.
- **„Vorherige Temperatur wiederherstellen" (`window_legacy_restore`) entfernt:** Das Ziel wird beim Fensterschließen ohnehin frisch berechnet.
- **Vier wirkungslose Optionen entfernt:** `sync_adjustments`, `sync_calendar_with_entities`, `valve_timeout`, `climate_rate_lookback` (nie implementiert).

Gespeicherte Werte der entfernten Optionen werden ignoriert; es ist nichts zu tun.

### 🔬 Unter der Haube
- **Neue Entscheidungs-Engine übernimmt die Steuerung:** Sie lief zuvor über Monate parallel zum alten System und wurde Entscheidung für Entscheidung verglichen (2880 Szenario-Kombinationen). Das alte System rechnet im Hintergrund weiter mit und meldet Abweichungen ins Log; Zurückschalten ist eine Zeile.
- **Robuster bei Geräteausfällen:** Antwortet ein Thermostat wiederholt nicht, wird es mit wachsenden Pausen (2 → 4 → … → 120 Min.) zurückgestellt. Tempix wacht exakt zum nächsten Zeitplan-Wechsel auf.
- **Mindestversion ist Home Assistant 2024.7.0** (bisher angegeben: 2024.6.0). Die Zeitraum-Anzeige aus Helper-Zeiten und die Feiertags-/Urlaubs-Umleitung im Helper-Modus ohne hinterlegten Kalender brauchen 2025.3 (`schedule.get_schedule`); darunter bleibt ein solcher Raum an einem umgeleiteten Tag auf Eco.

### 🔧 Code Quality
- **Geräte-Identität aus einer einzigen Quelle** (die Sensor-Plattform meldete als Modell „Virtual Thermostat", die übrigen „Tempix"); ein Test schlägt bei Abweichung an.
- **`decision_logic` im Diagnose-Export** trennt `schedule_*` (ohne Override) und `effective_*` (real gefahren) und trägt `override_active`.

## [1.6.7] - 2026-07-09

### ✨ New Features
- **Climate-Karte jetzt direkt steuerbar:** Das Tempix-Thermostat-Entity war bisher eine reine Anzeige (Set-Calls waren wirkungslos). Ein Ziehen am Temperatur-Slider oder Ändern des HVAC-Modus direkt in der Karte löst jetzt (sofern `enable_temporary_manual_override` aktiv ist) einen Temporary Override aus — genau wie ein manueller Eingriff am physischen Thermostat. Während einer aktiven Pause (`switch.manual_override`) werden Set-Calls stattdessen 1:1 an alle Hardware-TRVs durchgereicht.

### 🐛 Bug Fixes
- **Klimagerät kühlte/heizte bei offenem Fenster gegen die Untergrenze statt auszugehen:** War die Fenster-Offen-Temperatur auf `0` konfiguriert (= "Gerät aus"), berechnete die Engine trotzdem den Komfort-Modus. Das dabei entstehende 0-Ziel wurde anschließend auf das Geräteminimum hochgeklemmt — im Ergebnis lief z. B. die Klimaanlage bei weit geöffnetem Fenster aktiv auf 16 °C. Bei `window_open_temp = 0` schaltet das Gerät jetzt korrekt ab (Ausnahme: aktiver Frostschutz hält im Heizbetrieb die Frost-Mindesttemperatur).

## [1.6.6] - 2026-07-07

Enthält auch alle Änderungen von 1.6.5, das nie veröffentlicht wurde.

### ✨ New Features
- **Temporary Override:** Manuelle Temperaturänderungen direkt am Thermostat werden erkannt; die Automatik pausiert bis zur nächsten Zeitplan-Änderung oder bis der Override aufgehoben wird. Ein neuer Schalter hebt ihn jederzeit auf, die Option `enable_temporary_manual_override` schaltet die Erkennung ein oder aus.
- **Der Hysterese-Zustand der Außentemperatur überlebt Neustarts;** die erste Prüfung nach der Installation rechnet ohne Hysterese-Verzerrung.

### ⚠️ Breaking Changes
- **Die veralteten Optionen `ui_change`, `physical_change` und `reset_temperature` sind entfernt:** Manuelle Anpassungen laufen ausschließlich über das Temporary-Override-System.

### 🐛 Bug Fixes
- **Temporary Override endet pünktlich zum eigenen Slot-Beginn (geteilte Kalender):** Der nächste Phasenwechsel wird aus den raumgefilterten Terminen berechnet statt aus dem Event eines anderen Raums; Smart Pre-Conditioning zielt ebenfalls auf raumeigene Termine.
- **Keine falschen Overrides mehr durch zweigeteilte Geräte-Bestätigungen** (z. B. Zigbee2MQTT bestätigt Modus und Temperatur getrennt).
- **Manuelle Eingaben direkt nach einem Override-Ende gehen nicht mehr verloren:** Das 15-Sekunden-Schutzfenster unterdrückt nur noch Echos des beendeten Overrides.
- **Offenes Fenster übersteuert den Override (wie dokumentiert):** Der Override wird unterdrückt, nicht gelöscht; der Status zeigt `Window Open` bzw. `Frost Protection`.
- **Hysterese-Ping-Pong im Kühlmodus:** Überschneidende Ein-/Ausschaltgrenzen ließen das Gerät hin- und herschalten und auf die Minimaltemperatur fallen. Behoben.
- **Integration nach einem Update nicht mehr „leer" bis zum Neustart;** Options-Änderungen wirken sofort vollständig (Kalibrierung, Ventil, Lernmodul).
- **Absturz beim Entladen oder Neuladen der Integration** (`KeyError`) behoben.
- **Der Sensor „Anpassungen (JSON)"** liefert wieder den vollständigen JSON-String; sein Name ist korrigiert.
- **Die Climate-Karte zeigt keine veralteten Wunschwerte mehr** und der Temperatur-Slider verschwindet nicht mehr (Fallback 20 °C beim Umschalten von Aus auf Heizen/Kühlen ohne Zielwert).
- **Party-/Gäste-Timer überleben Stromausfälle** (sofort gespeichert statt nur beim sauberen Herunterfahren).
- **Ein Absturz im Debug-Logging bei unsicherem Sensorzustand** brach zusätzlich den beschleunigten 30-Sekunden-Zyklus ab. Behoben. Der Override-Schalter aktualisiert seine Anzeige beim Ausschalten sofort.

### 🎨 Improvements
- **Neutrale Bezeichnungen:** „Klima-Regelung", „Regelung" und „Intelligentes Vorheizen/Vorkühlen" statt „Heizung", da Tempix auch Kühlgeräte steuert.
- **Abwesenheits-Verhalten als ein Dropdown `away_behavior`** (`offset`, `eco`, `off`, `ignore`) statt der Checkboxen `off_if_nobody_home`, `away_use_eco_temp` und `away_scheduler_mode`; Standard ist `ignore` (der Zeitplan gilt unverändert).
- **Deutlich weniger Schreibzugriffe auf die Speicherkarte** (Circuit-Breaker-Status nur bei Änderung; relevant für Raspberry Pi). Kein Log-Spam bei deaktiviertem Override-Feature (eine Meldung pro Start). `TURN_OFF`/`TURN_ON` ergänzt (behebt die Home-Assistant-Deprecation-Warnung).

## [1.6.4] - 2026-05-26

### 🐛 Bug Fixes
- **Feiertags-Erkennung war einen Tag zu lang aktiv:** Das exklusive iCal-Enddatum ganztägiger Feiertage wurde als inklusiv gewertet, `holiday_use_day` blieb so den ganzen Folgetag aktiv.

### 🎨 Improvements
- **HACS-Release als ZIP:** Jede Version enthält jetzt automatisch das Asset `tempix.zip` (`zip_release` in `hacs.json`).

## [1.6.3] - 2026-05-06

### ✨ New Features
- **Kalender-Temperatur im Status:** `Comfort (📅 21°C)` / `Eco (📅 18°C)`, wenn ein Kalender-Event die Temperatur vorgibt.
- **Kalender-Tag-Kommentare:** Zeilen mit `#` in Kalender-Beschreibungen werden ignoriert.
- **Neues Attribut `heating_state`** am Status-Sensor (13 definierte Zustände).

### 🐛 Bug Fixes
- **Feiertags-Erkennung einen Tag zu früh:** Ganztägige Events (z. B. Maifeiertag) wurden ab Mitternacht Ortszeit statt Mitternacht UTC erkannt; es gilt jetzt die lokale Zeitzone.
- **Der Status-Sensor zeigt beim Kühlen `Outside < Threshold`,** wenn die Kühlung wegen zu niedriger Außentemperatur inaktiv ist.
- **Entities nach einem Neustart ausgegraut:** Alle Plattformen melden sich korrekt beim Coordinator an.
- **Party-Modus und Fenster-Szene:** Nach Ende der Party wurde die Fenster-Szene fälschlich wiederhergestellt.
- **Wetter-Entity, Feiertagskalender und in Adjustments referenzierte Entities** lösen jetzt sofort eine Neuberechnung aus (Sunshine-Offset, Feiertags-Übergänge, Adjustments), statt erst beim nächsten Heartbeat.
- **Config-Dialog:** Kalkschutz-Zeit als Zeitauswahl statt Freitext, Proximity als Geräte-Auswahl statt Entity-Auswahl.
- **Diagnose-Export:** Titel von Kalender-Events werden entfernt, nur Start- und Endzeiten bleiben.

### 🎨 Improvements
- **Der Circuit-Breaker-Zustand für fehlerhafte Thermostate überlebt Neustarts,** und die gelernte Heizrate liegt in einem eigenen Speicher (kein Reload mehr dadurch). Szenen-Snapshots werden pro Raum statt global gespeichert.
- **Parallele Aktualisierungen** (z. B. vier Schalter gleichzeitig) werden zusammengefasst und halbieren den Funkverkehr zu den Thermostaten.
- **Warnung im Log bei nicht auflösbaren Zielen** von `use_day`, `use_scheduler` und `holiday_use_day`.

## [1.6.2] - 2026-04-09

### ✨ New Features
- **Feiertags-Erkennung:** Die Parameter `holiday_calendar` (z. B. `calendar.holidays_in_germany`) und `holiday_use_day` behandeln einen Feiertag wie den konfigurierten Wochentag, in beiden Modi (Helper-Modus über `schedule.get_schedule`, Kalender-Modus über die Delegierungs-Logik).

### 🎨 Improvements & Changes
- **Vollständige Zeitanzeige im Helper-Modus:** „Active Scheduler Time Period" zeigt `06:00 - 21:30` statt nur `Heizen bis 21:30`; Feiertage mit Wochentag-Suffix (`06:00 - 21:30 (So.)`).
- **Sunshine Offset umbenannt:** `weather_anticipation` → `sunshine_offset`, `weather_offset` → `sunshine_offset_value`. Der Offset greift nur noch bei `sunny` (nicht mehr bei `clear`), Status `Comfort (☀️ -1.0°C)`.
- **Status-Verbesserungen:** `Outside > Threshold` auch im Eco-Status (nur bei aktivem Saisonmodus), Feiertag vor Wetter, Away mit Emoji (`Away (🚶 -1.0°C)`).

### 🐛 Bug Fixes
- **Kalender-Delegation ohne Location** (z. B. Google Calendar): Es wird nach dem Titel gefiltert, damit die Raum-Zuordnung auch ohne Ortsangabe funktioniert.

## [1.6.0] - 2026-03-03

### 🐛 Bug Fixes
- **Frostschutz greift jetzt auch im Saisonmodus** (wird vor der Automatik-Prüfung ausgewertet).
- **Eigenes Feld `weather_entity`** für die Wetter-Vorwegnahme; der Außensensor akzeptiert nur noch `sensor`-Entities.
- **Multi-Sensor-Fenstererkennung:** Ein ausgefallener Sensor blockiert nicht mehr; bei mindestens 50 % gültiger Sensoren entscheiden diese, ein bestätigter „offen"-Sensor gewinnt immer.
- **Party endet bei offenem Fenster:** Die Fenster-Szene mit Party-Temperaturen wird verworfen statt wiederhergestellt; nach einem Neustart werden veraltete Fenster-Szenen ebenfalls verworfen.
- **Einmalige Warnung im Log,** wenn die Kalibrier-Entity am Gerät fehlt oder die Gästemodus-Entity nicht verfügbar ist.
- **Force Comfort und Force Eco schließen sich gegenseitig aus:** Einschalten des einen schaltet das andere aus.

### 🎨 Improvements & Changes
- **Wetter-Vorwegnahme** greift nur noch im Comfort-Modus und bei Tages-Wetterzuständen (`sunny`, `clear`); `clear-night` entfällt.
- **Hysterese-Standard von 0,2 °C auf 0,3 °C erhöht** (passend für TRVs mit 0,5-°C-Stufen, z. B. FRITZ!DECT Comet).
- **Kalibrierung `generic` ohne Entity** loggt `DEBUG` statt `WARNING` (bei FRITZ!DECT erwartet); `native` ohne Entity bleibt `WARNING`.

## Frühere Versionen (1.1.0 – 1.5.9)
- **1.1.6:** Außentemperatur-Hysterese `outside_temp_hysteresis` (0–5 °C, Standard 1,0 °C) gegen schnelles Hin- und Herschalten an der Schwelle. Optionale Entity-Felder lassen sich im Options-Dialog wieder löschen.
- **1.1.3:** Schalter wirken sofort statt erst beim nächsten Heartbeat (60 s). Der Sensor „Active Scheduler" zeigte im Helper-Modus fälschlich den Kalendernamen.
- **1.1.2:** Die externen `input_number`-Helper für Comfort/Eco entfallen; Tempix nutzt eigene Number-Entities.
- **1.1.1:** Neue Standardwerte (Aggressiv-Bereich 0,3, Aggressiv-Offset 1,0, Wetter-Offset 1,0, Party-Temperatur 18 °C). Neu angelegte Einträge bekamen keine Entities, behoben.
- **1.1.0:** Diagnose-Sensoren `TRV Temp` und `TRV Target`, Debug-Mode-Schalter, Schalter zum Pausieren der Hauptschleife, zusammengeführter Einrichtungsdialog, automatische Erkennung von °C/°F. Behoben: Kalibrier-Drift bei Thermostaten im Modus „off" und Fließkomma-Fehler im Kalkschutz.
