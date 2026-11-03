"""UserProfile, Randbedingungen (Constraints) und Reise-Historie."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .common import ActivityCategory, Intensity, Model, Money, new_id

# +2 = sehr gern, 0 = egal, -2 = auf keinen Fall (Veto für den ganzen Haushalt)
Preference = Annotated[int, Field(ge=-2, le=2)]

# Vorgabe, was in der Schwangerschaft automatisch vermieden wird. Keine medizinische Aussage:
# mit Ärztin/Hebamme abstimmen und über ``PregnancyStatus.extra_avoid_tags`` ergänzen.
PREGNANCY_AVOID_TAGS = frozenset({"hitze", "tauchen", "hoehe", "achterbahn"})


class ClimatePreference(Model):
    temp_min_c: float = 18
    temp_max_c: float = 28
    rain_tolerance: Literal["gering", "mittel", "hoch"] = "mittel"

    @model_validator(mode="after")
    def _range(self):
        if self.temp_min_c > self.temp_max_c:
            raise ValueError("temp_min_c ist grösser als temp_max_c")
        return self


class BudgetPreference(Model):
    per_night_max: Money | None = None
    per_day_activities_max: Money | None = None
    currency: str = "CHF"


class UserProfile(Model):
    id: str = Field(default_factory=new_id)
    name: str = Field(min_length=1, max_length=60)
    role: Literal["haupt", "partnerin"]
    activities: dict[ActivityCategory, Preference] = {}
    climate: ClimatePreference = ClimatePreference()
    budget: BudgetPreference = BudgetPreference()
    languages: list[str] = ["de"]

    def preference(self, cat: ActivityCategory) -> int:
        return self.activities.get(cat, 0)


class PregnancyStatus(Model):
    due_date: date
    cleared_by_doctor: bool = False
    extra_avoid_tags: set[str] = set()

    def week_on(self, d: date) -> int:
        """Schwangerschaftswoche (vollendete Wochen) an einem Datum, aus dem Geburtstermin geschätzt."""
        return max(0, (280 - (self.due_date - d).days) // 7)


class TravelConstraints(Model):
    """Feste Randbedingungen – gelten für jede Planung und jede Planänderung."""

    budget_total_max: Money | None = None
    currency: str = "CHF"
    duration_days_min: int = Field(default=1, ge=1)
    duration_days_max: int = Field(default=21, ge=1)
    max_flight_hours: float | None = Field(default=None, gt=0)
    max_stops: int = Field(default=1, ge=0, le=3)
    max_intensity: Intensity = Intensity.ANSTRENGEND
    avoid_tags: set[str] = set()
    pregnancy: PregnancyStatus | None = None

    @model_validator(mode="after")
    def _duration(self):
        if self.duration_days_min > self.duration_days_max:
            raise ValueError("duration_days_min ist grösser als duration_days_max")
        return self

    def effective_avoid_tags(self) -> set[str]:
        tags = set(self.avoid_tags)
        if self.pregnancy:
            tags |= PREGNANCY_AVOID_TAGS | self.pregnancy.extra_avoid_tags
        return tags

    def effective_max_intensity(self) -> Intensity:
        if self.pregnancy:
            return min(self.max_intensity, Intensity.MITTEL)
        return self.max_intensity


class TravelHistory(Model):
    visited_countries: set[str] = set()

    @field_validator("visited_countries", mode="before")
    @classmethod
    def _norm(cls, v):
        out = {str(c).strip().upper() for c in v}
        bad = [c for c in out if len(c) != 2 or not c.isalpha()]
        if bad:
            raise ValueError(f"keine ISO-Ländercodes: {bad}")
        return out

    def is_new(self, country: str) -> bool:
        return country.upper() not in self.visited_countries


class Household(Model):
    """Beide Profile plus gemeinsame Randbedingungen und Historie."""

    id: str = Field(default_factory=new_id)
    profiles: list[UserProfile] = Field(min_length=1, max_length=2)
    constraints: TravelConstraints = TravelConstraints()
    history: TravelHistory = TravelHistory()

    @model_validator(mode="after")
    def _roles(self):
        roles = [p.role for p in self.profiles]
        if len(set(roles)) != len(roles):
            raise ValueError("jede Rolle nur einmal")
        return self

    def harmonized(self, cat: ActivityCategory) -> float | None:
        """Gemeinsame Vorliebe für eine Kategorie (erste Version des MatchingModule).

        None = Veto (eine Person hat -2 gesetzt), sonst Mittelwert beider Profile.
        """
        prefs = [p.preference(cat) for p in self.profiles]
        if min(prefs) <= -2:
            return None
        return sum(prefs) / len(prefs)
