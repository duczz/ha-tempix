"""Tempix – gemeinsame Geraete-Identitaet fuer alle Plattformen."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo

from custom_components.tempix.const import (
    DOMAIN,
    VERSION,
)

MANUFACTURER = "Martin Müller"
MODEL = "Tempix"


def tempix_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Die DeviceInfo, die JEDE Tempix-Plattform verwenden muss.

    Alle Plattformen registrieren ueber dieselben ``identifiers`` auf EIN
    Geraet, und HA leitet in ``device_registry.async_get_or_create`` jeden
    Aufruf unbedingt in den Update-Pfad — der letzte Schreiber gewinnt.
    Abweichende Werte pro Datei sind deshalb kein Schoenheitsfehler, sondern
    ein Race ueber die Plattform-Reihenfolge: frueher deklarierte
    ``sensor.py`` ``model="Virtual Thermostat"``, die uebrigen ``"Tempix"``,
    und welcher Wert ueberlebte, entschied allein, welche Plattform ihr Setup
    zuletzt beendete.

    Eine Quelle statt acht Kopien, damit dieselbe Divergenz nicht neu
    entstehen kann — eine reine Merkregel haette das nur
    dokumentiert, nicht verhindert.

    ``entry.title`` statt ``config.name``: nur der Titel folgt dem Umbenennen
    ueber HAs Kontextmenue. Siehe ``__init__.py``, wo ``entry.title`` auch in
    die Roh-Config gespeist wird, damit Logs und Repair-Issues nicht
    auseinanderlaufen.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer=MANUFACTURER,
        model=MODEL,
        sw_version=VERSION,
    )
