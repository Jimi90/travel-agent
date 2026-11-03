"""AlternativeActivitiesModule: findet wetterfeste Ersatz-Aktivitäten, die beiden gefallen.

Die Kandidaten liefert ein ``AlternativeProvider``. Schritt 1: fester Katalog (für Tests und
Offline). Später: Orte-API und/oder KI-Vorschläge – das Bewerten und Filtern bleibt hier.
"""
from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from typing import Protocol

from ..models import Activity, AlternativeCandidate, Exposure, Household, Place, WeatherForecast
from ..models.common import Model, from_min, to_min
from ..models.itinerary import DEFAULT_LIMITS


class ScoredAlternative(Model):
    candidate: AlternativeCandidate
    score: float
    reasons: list[str] = []


class AlternativeProvider(Protocol):
    def candidates(self, place: Place, day: date) -> list[AlternativeCandidate]: ...


class StaticCatalog:
    def __init__(self, items: list[AlternativeCandidate]):
        self.items = items

    def candidates(self, place: Place, day: date) -> list[AlternativeCandidate]:
        return [c for c in self.items if c.place.same_area(place)]


class AlternativeActivitiesModule:
    def __init__(self, provider: AlternativeProvider, household: Household):
        self.provider = provider
        self.household = household

    def suggest(
        self,
        *,
        place: Place,
        day: date,
        start: time,
        end: time,
        original: Activity | None = None,
        forecast: WeatherForecast | None = None,
        exclude_titles: set[str] = frozenset(),
        budget_left: Decimal | None = None,
        limit: int = 3,
    ) -> list[ScoredAlternative]:
        out = self._score(self.provider.candidates(place, day), place, day, start, end, original, forecast,
                          exclude_titles, budget_left)
        if not out and hasattr(self.provider, "more"):  # nichts passt: zusätzliche Vorschläge holen
            out = self._score(self.provider.more(place, day), place, day, start, end, original, forecast,
                              exclude_titles, budget_left)
        out.sort(key=lambda s: (-s.score, s.candidate.title))
        return out[:limit]

    def _score(self, candidates, place, day, start, end, original, forecast, exclude_titles,
               budget_left) -> list[ScoredAlternative]:
        k = self.household.constraints
        avoid, max_int = k.effective_avoid_tags(), k.effective_max_intensity()
        window = to_min(end) - to_min(start)
        excluded = {t.casefold() for t in exclude_titles}
        out: list[ScoredAlternative] = []
        for c in candidates:
            if c.title.casefold() in excluded or not c.fits(start, end):
                continue
            c_end = from_min(to_min(start) + c.duration_min)
            if forecast and c.exposure is not Exposure.INDOOR and forecast.problems_for(DEFAULT_LIMITS[c.exposure], start, c_end):
                continue
            if c.intensity > max_int or c.tags & avoid:
                continue
            pref = self.household.harmonized(c.category)
            if pref is None:  # Veto einer Person
                continue
            extra = c.cost - (original.cost if original else Decimal("0"))
            if budget_left is not None and extra > budget_left:
                continue

            score, why = pref * 2, [f"gemeinsame Vorliebe {pref:+.1f}"]
            if original and c.category == original.category:
                score += 1
                why.append("gleiche Art wie geplant")
            if c.exposure is Exposure.INDOOR:
                score += 0.5
                why.append("wetterfest")
            if extra > 0:
                score -= float(extra) / 50
                why.append(f"{extra} {k.currency} teurer")
            if original:
                score -= abs(int(c.intensity) - int(original.intensity)) * 0.3
            score += 0.5 * c.duration_min / window  # füllt die Lücke gut
            out.append(ScoredAlternative(candidate=c, score=round(score, 3), reasons=why))
        return out

    @staticmethod
    def to_activity(sa: ScoredAlternative, start: time, original: Activity | None, note: str) -> Activity:
        c = sa.candidate
        return Activity(
            title=c.title, category=c.category, exposure=c.exposure, intensity=c.intensity,
            start=start, end=from_min(to_min(start) + c.duration_min), place=c.place, cost=c.cost,
            tags=c.tags, origin="umplanung", replaces=original.id if original else None, note=note,
        )
