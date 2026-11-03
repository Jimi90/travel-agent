"""Reiseplan (Itinerary) mit Tagen, Aktivitäten, Unterkünften und Transporten."""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .common import ActivityCategory, Exposure, Intensity, Model, Money, Place, new_id, to_min


class WeatherLimits(Model):
    """Ab wann eine Aktivität wegen Wetter nicht mehr sinnvoll ist."""

    max_precip_mm_day: float = 5.0
    max_precip_mm_hour: float = 1.0
    max_precip_probability: int = Field(default=70, ge=0, le=100)
    max_wind_kmh: float = 45
    thunderstorm_ok: bool = False


DEFAULT_LIMITS: dict[Exposure, WeatherLimits] = {
    Exposure.OUTDOOR: WeatherLimits(),
    Exposure.MIXED: WeatherLimits(
        max_precip_mm_day=15, max_precip_mm_hour=4, max_precip_probability=100, max_wind_kmh=60
    ),
}


class Booking(Model):
    provider: str
    reference: str | None = None
    cancellable: bool = True
    cancel_deadline: datetime | None = None

    def can_cancel(self, now: datetime) -> bool:
        return self.cancellable and (self.cancel_deadline is None or now < self.cancel_deadline)


class Activity(Model):
    id: str = Field(default_factory=new_id)
    title: str = Field(min_length=1, max_length=160)
    category: ActivityCategory
    exposure: Exposure
    intensity: Intensity = Intensity.LEICHT
    start: time
    end: time
    place: Place | None = None
    cost: Money = Decimal("0")
    tags: set[str] = set()
    fixed: bool = False  # von Hand festgenagelt: die Automatik fasst es nie an
    booking: Booking | None = None
    weather_limits: WeatherLimits | None = None  # None = Vorgabe nach ``exposure``
    origin: Literal["plan", "umplanung", "nutzer"] = "plan"
    replaces: str | None = None  # ID der ersetzten Aktivität (für Nachvollziehbarkeit)
    note: str | None = None

    @model_validator(mode="after")
    def _times(self):
        if self.end <= self.start:
            raise ValueError(f"«{self.title}»: Ende muss nach dem Beginn liegen")
        return self

    @property
    def duration_min(self) -> int:
        return to_min(self.end) - to_min(self.start)

    def limits(self) -> WeatherLimits | None:
        if self.exposure is Exposure.INDOOR:
            return None
        return self.weather_limits or DEFAULT_LIMITS[self.exposure]

    def locked(self, now: datetime) -> bool:
        """Darf die Automatik diese Aktivität nicht verändern?"""
        return self.fixed or (self.booking is not None and not self.booking.can_cancel(now))


class DayPlan(Model):
    date: date
    base: Place  # Aufenthaltsort an diesem Tag
    activities: list[Activity] = []

    @field_validator("activities")
    @classmethod
    def _sorted(cls, v: list[Activity]) -> list[Activity]:
        return sorted(v, key=lambda a: a.start)


class Accommodation(Model):
    id: str = Field(default_factory=new_id)
    name: str
    place: Place
    check_in: date
    check_out: date
    cost: Money = Decimal("0")
    booking: Booking | None = None

    @model_validator(mode="after")
    def _dates(self):
        if self.check_out <= self.check_in:
            raise ValueError(f"«{self.name}»: Abreise muss nach der Anreise liegen")
        return self


class TransportSegment(Model):
    id: str = Field(default_factory=new_id)
    mode: Literal["flug", "faehre", "zug", "bus", "auto"]
    origin: Place
    destination: Place
    depart: datetime
    arrive: datetime
    carrier: str | None = None
    number: str | None = None
    cost: Money = Decimal("0")
    booking: Booking | None = None

    @model_validator(mode="after")
    def _times(self):
        if self.arrive <= self.depart:
            raise ValueError("Ankunft muss nach der Abfahrt liegen")
        return self


class Itinerary(Model):
    id: str = Field(default_factory=new_id)
    title: str = Field(min_length=1, max_length=160)
    household_id: str | None = None
    start_date: date
    end_date: date
    days: list[DayPlan] = []
    stays: list[Accommodation] = []
    transports: list[TransportSegment] = []
    status: Literal["entwurf", "gebucht", "unterwegs", "abgeschlossen"] = "entwurf"
    version: int = Field(default=1, ge=1)  # wird nur vom StatusManager erhöht
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def _days(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date liegt vor start_date")
        dates = [d.date for d in self.days]
        if len(set(dates)) != len(dates):
            raise ValueError("ein Datum kommt mehrfach vor")
        for d in dates:
            if not self.start_date <= d <= self.end_date:
                raise ValueError(f"Tag {d} liegt ausserhalb der Reise")
        self.days.sort(key=lambda d: d.date)
        ids = [a.id for d in self.days for a in d.activities]
        if len(set(ids)) != len(ids):
            raise ValueError("Aktivitäts-ID doppelt")
        return self

    def day(self, d: date) -> DayPlan | None:
        return next((x for x in self.days if x.date == d), None)

    def find_activity(self, activity_id: str) -> tuple[DayPlan, Activity] | None:
        for d in self.days:
            for a in d.activities:
                if a.id == activity_id:
                    return d, a
        return None

    def total_cost(self) -> Decimal:
        return (
            sum((a.cost for d in self.days for a in d.activities), Decimal("0"))
            + sum((s.cost for s in self.stays), Decimal("0"))
            + sum((t.cost for t in self.transports), Decimal("0"))
        )


class SavedItinerary(Model):
    """Favorit: Verweis auf einen gespeicherten Plan (die Daten liegen im StatusManager)."""

    itinerary_id: str
    label: str = Field(min_length=1, max_length=80)
    saved_at: datetime
    note: str | None = None
