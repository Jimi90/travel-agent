"""Gemeinsame Grundtypen für alle Datenmodelle.

Zeiten (``time``/``datetime`` ohne Zeitzone) gelten immer als Ortszeit am Aufenthaltsort.
Beträge sind ``Decimal`` (keine Rundungsfehler bei Geld), Standardwährung CHF.
"""
from __future__ import annotations

from datetime import time
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def new_id() -> str:
    return uuid4().hex[:12]


def to_min(t: time) -> int:
    return t.hour * 60 + t.minute


def from_min(m: int) -> time:
    return time(m // 60, m % 60)


class Model(BaseModel):
    """Basis: unbekannte Felder sind ein Fehler (Tippfehler fallen sofort auf)."""

    model_config = ConfigDict(extra="forbid")


Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]


class ActivityCategory(StrEnum):
    WANDERN = "wandern"
    NATUR = "natur"
    STRAND = "strand"
    WASSERSPORT = "wassersport"
    KULTUR = "kultur"
    MUSEUM = "museum"
    STADT = "stadt"
    KULINARIK = "kulinarik"
    WELLNESS = "wellness"
    ENTSPANNUNG = "entspannung"
    SHOPPING = "shopping"
    NACHTLEBEN = "nachtleben"


class Exposure(StrEnum):
    """Wie stark eine Aktivität vom Wetter abhängt."""

    INDOOR = "indoor"
    OUTDOOR = "outdoor"
    MIXED = "gemischt"


class Intensity(IntEnum):
    LEICHT = 1
    MITTEL = 2
    ANSTRENGEND = 3


class Place(Model):
    name: str = Field(min_length=1, max_length=120)
    country: str = Field(pattern=r"^[A-Z]{2}$", description="ISO-3166-Ländercode, z. B. PT")
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    tz: str | None = Field(default=None, max_length=64, description="IANA-Zeitzone, z. B. Europe/Lisbon")

    @field_validator("country", mode="before")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper() if isinstance(v, str) else v

    def same_area(self, other: Place) -> bool:
        return self.country == other.country and self.name.casefold() == other.name.casefold()


CATEGORY_LABELS = {
    ActivityCategory.WANDERN: "Wandern", ActivityCategory.NATUR: "Natur", ActivityCategory.STRAND: "Strand",
    ActivityCategory.WASSERSPORT: "Wassersport", ActivityCategory.KULTUR: "Kultur", ActivityCategory.MUSEUM: "Museen",
    ActivityCategory.STADT: "Städte", ActivityCategory.KULINARIK: "Essen & Trinken",
    ActivityCategory.WELLNESS: "Wellness & Spa", ActivityCategory.ENTSPANNUNG: "Entspannung",
    ActivityCategory.SHOPPING: "Shopping", ActivityCategory.NACHTLEBEN: "Ausgang",
}
