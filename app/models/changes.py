"""Schemas des StatusManagers: Planänderungen als Operationen, Ergebnis und Protokoll.

Jede Änderung am Reiseplan ist ein ``ChangeSet`` (Liste von Operationen auf einer bestimmten
Plan-Version). Der StatusManager wendet es ganz oder gar nicht an.
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import Field

from .common import Model, new_id
from .common import Place
from .itinerary import Accommodation, Activity, TransportSegment


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


class ExtendStay(Model):
    """Unterkunft verlängern (neues Abreisedatum)."""

    op: Literal["unterkunft_verlaengern"] = "unterkunft_verlaengern"
    stay_id: str
    new_check_out: date


class AddStay(Model):
    op: Literal["unterkunft_hinzufuegen"] = "unterkunft_hinzufuegen"
    stay: Accommodation


class ShiftSchedule(Model):
    """Alles ab ``from_date`` um ``days`` Tage nach hinten schieben (Tage, Unterkünfte, Transporte, Reiseende).

    Die entstehende Lücke wird mit leeren Tagen am Ort ``gap_base`` gefüllt.
    """

    op: Literal["plan_verschieben"] = "plan_verschieben"
    from_date: date
    days: int = Field(ge=1, le=14)
    gap_base: Place


class SetTripStatus(Model):
    op: Literal["status_setzen"] = "status_setzen"
    status: Literal["entwurf", "gebucht", "unterwegs", "abgeschlossen"]


class SetFixed(Model):
    """Aktivität festnageln (die Automatik fasst sie nie an) oder wieder freigeben."""

    op: Literal["festnageln"] = "festnageln"
    activity_id: str
    fixed: bool


class SetConfirmed(Model):
    """Unterkunft/Verbindung als gebucht markieren (oder wieder als Vorschlag)."""

    op: Literal["bestaetigen"] = "bestaetigen"
    target: Literal["unterkunft", "verbindung"]
    item_id: str
    confirmed: bool
    reference: str | None = None


class UpdateStay(Model):
    """Unterkunft mit echten Daten aktualisieren (Buchungsbestätigung, echte Preise). None = unverändert."""

    op: Literal["unterkunft_aendern"] = "unterkunft_aendern"
    stay_id: str
    name: str | None = None
    check_in: date | None = None
    check_out: date | None = None
    cost: Decimal | None = Field(default=None, ge=0)
    confirmed: bool | None = None
    provider: str | None = None
    reference: str | None = None
    search_url: str | None = None
    note: str | None = None


class UpdateTransport(Model):
    """Verbindung mit echten Daten aktualisieren. None = unverändert."""

    op: Literal["verbindung_aendern"] = "verbindung_aendern"
    segment_id: str
    depart: datetime | None = None
    arrive: datetime | None = None
    carrier: str | None = None
    number: str | None = None
    via: list[str] | None = None
    origin_code: str | None = None
    destination_code: str | None = None
    cost: Decimal | None = Field(default=None, ge=0)
    confirmed: bool | None = None
    reference: str | None = None
    search_url: str | None = None
    note: str | None = None


class AddTransport(Model):
    op: Literal["verbindung_hinzufuegen"] = "verbindung_hinzufuegen"
    segment: TransportSegment


PlanOperation = Annotated[
    Union[ReplaceActivity, MoveActivity, AddActivity, RemoveActivity, ExtendStay, AddStay, ShiftSchedule,
          SetTripStatus, SetFixed, SetConfirmed, UpdateStay, UpdateTransport, AddTransport],
    Field(discriminator="op"),
]


class ExternalAction(Model):
    """Etwas, das ausserhalb der App passieren muss (Storno, Anfrage, Nachricht).

    Wird im selben Schritt wie die Planänderung in die «Outbox» geschrieben und erst danach
    ausgeführt – so gibt es nie eine Buchungsänderung ohne Planänderung oder umgekehrt.
    """

    id: str = Field(default_factory=new_id)
    kind: Literal["buchung_stornieren", "buchung_aendern", "buchung_anfragen", "hotel_verlaengern",
                  "transport_umbuchen", "nachricht"]
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
    # Feste/gebuchte Aktivitäten, die dieses ChangeSet bewusst verändert (z. B. Plan nach hinten schieben).
    # Für jede muss eine ExternalAction mit Bestätigung beiliegen.
    acknowledged_locked: list[str] = []


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
