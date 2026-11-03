"""Schemas des StatusManagers: Planänderungen als Operationen, Ergebnis und Protokoll.

Jede Änderung am Reiseplan ist ein ``ChangeSet`` (Liste von Operationen auf einer bestimmten
Plan-Version). Der StatusManager wendet es ganz oder gar nicht an.
"""
from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import Field

from .common import Model, new_id
from .itinerary import Activity


class ReplaceActivity(Model):
    op: Literal["ersetzen"] = "ersetzen"
    activity_id: str
    new: Activity


class MoveActivity(Model):
    op: Literal["verschieben"] = "verschieben"
    activity_id: str
    to_day: date
    start: time
    end: time


class AddActivity(Model):
    op: Literal["hinzufuegen"] = "hinzufuegen"
    day: date
    activity: Activity


class RemoveActivity(Model):
    op: Literal["entfernen"] = "entfernen"
    activity_id: str


# Später (ExtendedDisruptionHandler): ExtendStay, ShiftDays, RebookTransport.
PlanOperation = Annotated[
    Union[ReplaceActivity, MoveActivity, AddActivity, RemoveActivity], Field(discriminator="op")
]


class ExternalAction(Model):
    """Etwas, das ausserhalb der App passieren muss (Storno, Anfrage, Nachricht).

    Wird im selben Schritt wie die Planänderung in die «Outbox» geschrieben und erst danach
    ausgeführt – so gibt es nie eine Buchungsänderung ohne Planänderung oder umgekehrt.
    """

    id: str = Field(default_factory=new_id)
    kind: Literal["buchung_stornieren", "buchung_aendern", "buchung_anfragen", "hotel_verlaengern", "nachricht"]
    description: str
    payload: dict[str, Any] = {}
    needs_confirmation: bool = True  # Geld/Buchungen nie ohne Bestätigung


class ChangeSet(Model):
    itinerary_id: str
    base_version: int = Field(ge=1)
    operations: list[PlanOperation] = Field(min_length=1)
    reason: str
    event_id: str | None = None  # gleiche Störung nie zweimal anwenden
    actor: Literal["autonom", "nutzer"] = "nutzer"
    external_actions: list[ExternalAction] = []


class ChangeStatus(StrEnum):
    APPLIED = "angewendet"
    REJECTED = "abgelehnt"  # Plausibilitätsprüfung oder ungültige Operation
    CONFLICT = "konflikt"  # Plan wurde inzwischen geändert (andere Version)
    DUPLICATE = "doppelt"  # Störung wurde schon verarbeitet
    NOOP = "nichts_zu_tun"


class Problem(Model):
    code: str
    message: str
    level: Literal["fehler", "warnung"] = "fehler"
    activity_id: str | None = None


class ChangeResult(Model):
    status: ChangeStatus
    itinerary_id: str
    old_version: int | None = None
    new_version: int | None = None
    change_id: str | None = None
    problems: list[Problem] = []


class ChangeRecord(Model):
    """Ein Eintrag im Änderungsprotokoll (Grundlage für «Rückgängig»)."""

    id: str
    itinerary_id: str
    from_version: int
    to_version: int
    event_id: str | None
    actor: str
    reason: str
    operations: list[dict[str, Any]]
    created_at: datetime
    undone_by: str | None = None
