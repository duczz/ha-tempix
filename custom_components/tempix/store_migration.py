"""Ordner-Migration für die Tempix-Stores (`.storage/tempix/`).

Tempix legte bis v1.7.0 **sieben Store-Dateien pro Raum** flach in `.storage/`
ab, dazu uneinheitlich benannt (vier mit Unterstrich, drei mit Punkt). Bei vier
Räumen sind das knapp 30 Dateien zwischen allen anderen HA-Stores.

Ein Unterordner ist technisch trivial: HA baut den Pfad als
``hass.config.path(STORAGE_DIR, key)`` und ruft beim Schreiben
``os.makedirs(os.path.dirname(path), exist_ok=True)`` — ein Key
``tempix/<name>_<entry>`` legt den Ordner also selbst an. Ein *Umbenennen*
kennt die Store-API dagegen nicht.

**Warum kein Einmal-Umzug (alt lesen → neu schreiben → alt löschen).**
Der Gewinn ist rein kosmetisch, das Risiko wäre echt: Ein Fehler zwischen
Schreiben und Löschen kostet Daten, und betroffen wäre unter anderem
``domain_state`` — aktive Overrides, Party-/Gast-Timer, Schedule-Fingerprint.
Kosmetik rechtfertigt keinen stillen Datenverlust.

**Deshalb zweiphasig.** Diese Klasse ist Phase A und kennt keinen Löschpfad:

* ``async_load`` liest den **neuen** Key. Kommt nichts, wird der **alte**
  gelesen — und der gefundene Stand sofort an den neuen Ort geschrieben.
  Das ist der eigentliche Umzug; ohne ihn bliebe ein Store, der gelesen aber
  nie wieder geschrieben wird (etwa die Hysterese, solange ``outside_ok``
  konstant bleibt), für immer nur am alten Ort liegen.
* Geschrieben wird **ausschließlich** an den neuen Ort.
* Die Altdatei bleibt unangetastet.

Damit gibt es keinen Moment, in dem Daten nur an einem noch nicht
geschriebenen Ort existieren. Phase B (ein Release später) räumt die Waisen
per ``Store.async_remove()`` ab — dann unter der bis dahin belegten Annahme,
dass die neue Datei existiert (noch nicht umgesetzt).

**Rollback-Hinweis.** Der Cutover-Rollback (``ACTIVE_ENGINE`` zurück auf
``"v1"``) ist von alldem **nicht** betroffen: gleiche Version, gleiche Keys.
Nur ein echtes Versions-Downgrade auf ≤ 1.6.x liest wieder die Altdateien und
sieht damit den Stand vor dem Umzug.

**Der Ordnername ist bewusst ein Literal, nicht ``DOMAIN``.** Alle Store-Keys
schreiben ``tempix`` fest aus, statt es aus der Domain-Konstante abzuleiten.
Das ist kein Versehen und darf nicht „aufgeräumt" werden:

* Beim letzten Umbenennen (``advanced_heating_control`` → ``tempix``) hing der
  Szenen-Store am Domain-Namen — und musste deshalb per Skript mitgezogen
  werden. Genau diese Kopplung fehlt hier jetzt.
* Würde man ``f"{DOMAIN}/…"`` schreiben, verschöbe eine spätere Umbenennung
  **alle sieben Speicher pro Raum auf einen Schlag** — aktive Overrides,
  Timer, Ledger, gelernte Rate. Nichts davon würde gefunden, nichts würde
  einen Fehler werfen: die Integration startet einfach frisch.
* Daten sollen die Produktbezeichnung überleben. Der Ordner ist ein stabiler
  Bezeichner, kein Anzeigename.

**Umbenennen ist erlaubt — sich selbst umbenennen nicht.** Verboten ist die
*Ableitung*, nicht der Name. Wird die Integration je umbenannt und soll der
Ordner mit, dann als **bewusster Schritt**: neuen Key eintragen, alten als
``legacy_store`` stehenlassen, eine Version später aufräumen — dieselbe
Zweiphasigkeit wie hier. Das kostet eine Zeile pro Store und keinen
Datenverlust. Was NICHT funktioniert: den Ordner auf der Platte umbenennen,
ohne die Keys mitzuziehen — der Ordnername lebt im Code, nicht im Dateisystem.

Ein Test pinnt die Entkopplung: er patcht ``DOMAIN`` und prüft, dass sich kein Key bewegt.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.core import callback

_LOGGER = logging.getLogger(__name__)


class MigratingStore:
    """Liest neu-oder-alt, schreibt nur neu. API-kompatibel zu HA's ``Store``.

    Bewusst eine Hülle um zwei fertige ``Store``-Instanzen statt einer
    Unterklasse: die Aufrufstellen konstruieren ihre Stores weiterhin selbst,
    damit die bestehenden Test-Patches auf ``coordinator.Store`` greifen und
    Tests, die das Attribut komplett durch ein ``AsyncMock`` ersetzen,
    unverändert laufen.
    """

    def __init__(self, store: Any, legacy_store: Any, name: str = "") -> None:
        self._store = store
        self._legacy = legacy_store
        self._name = name
        #: True, sobald einmal aus der Altdatei gelesen wurde (Tests).
        self.migrated_from_legacy = False

    @property
    def key(self) -> str:
        """Key des NEUEN Ortes — der ist ab jetzt der maßgebliche."""
        return getattr(self._store, "key", "")

    async def async_load(self) -> Any:
        """Neuen Ort lesen; sonst den alten und den Fund sofort umziehen."""
        data = await self._store.async_load()
        if data is not None:
            return data

        legacy = await self._legacy.async_load()
        if legacy is None:
            return None

        self.migrated_from_legacy = True
        _LOGGER.info(
            "%s: Store '%s' aus dem alten Ablageort uebernommen",
            self._name or "Tempix", self.key,
        )
        try:
            await self._store.async_save(legacy)
        except Exception as exc:  # noqa: BLE001 — Umzug darf das Laden nie brechen
            # Der Rueckgabewert stimmt trotzdem: gelesen ist gelesen. Der Umzug
            # wird beim naechsten Start erneut versucht.
            _LOGGER.warning(
                "%s: Store '%s' konnte nicht an den neuen Ort geschrieben "
                "werden (wird beim naechsten Start erneut versucht): %s",
                self._name or "Tempix", self.key, exc,
            )
        return legacy

    async def async_save(self, data: Any) -> None:
        await self._store.async_save(data)

    @callback
    def async_delay_save(
        self, data_func: Callable[[], Any], delay: float = 0
    ) -> None:
        self._store.async_delay_save(data_func, delay)
