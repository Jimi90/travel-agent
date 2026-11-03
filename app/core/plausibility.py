"""MiddlewareConfig: Plausibilitätsprüfung vor jeder Planänderung.

Vergleicht den Plan vorher/nachher. «fehler» blockiert die Änderung, «warnung» wird nur gemeldet.
Wird zweimal benutzt: beim Planen (welche Option ist zulässig?) und im StatusManager als letzte Sperre.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from ..models import Activity, Itinerary, Problem, TravelConstraints
from ..models.common import to_min


@dataclass(frozen=True)
class MiddlewareConfig:
    day_start: time = time(7, 0)
    day_end: time = time(23, 0)
    max_activities_per_day: int = 6
    min_gap_minutes: int = 0
    allow_budget_overrun: bool = False


class PlausibilityChecker:
    def __init__(self, config: MiddlewareConfig | None = None):
        self.config = config or MiddlewareConfig()

    def check(
        self,
        before: Itinerary,
        after: Itinerary,
        constraints: TravelConstraints | None,
        now: datetime,
    ) -> list[Problem]:
        out: list[Problem] = []
        out += self._days(after)
        out += self._locked_untouched(before, after, now)
        if constraints:
            out += self._constraints(before, after, constraints)
            out += self._budget(before, after, constraints)
        return out

    # --- einzelne Prüfungen -------------------------------------------------------------------

    def _days(self, itin: Itinerary) -> list[Problem]:
        c, out = self.config, []
        for day in itin.days:
            if len(day.activities) > c.max_activities_per_day:
                out.append(Problem(code="zu_voll", message=f"{day.date}: mehr als {c.max_activities_per_day} Aktivitäten"))
            prev: Activity | None = None
            for a in day.activities:
                if a.start < c.day_start or a.end > c.day_end:
                    out.append(Problem(code="ausserhalb_tag", activity_id=a.id,
                                       message=f"«{a.title}» liegt ausserhalb {c.day_start:%H:%M}–{c.day_end:%H:%M}"))
                if prev and to_min(a.start) < to_min(prev.end) + c.min_gap_minutes:
                    out.append(Problem(code="ueberschneidung", activity_id=a.id,
                                       message=f"{day.date}: «{prev.title}» und «{a.title}» überschneiden sich"))
                prev = a
        return out

    def _locked_untouched(self, before: Itinerary, after: Itinerary, now: datetime) -> list[Problem]:
        out = []
        for day in before.days:
            for a in day.activities:
                if not a.locked(now):
                    continue
                found = after.find_activity(a.id)
                if not found or found[0].date != day.date or found[1] != a:
                    out.append(Problem(code="fest_veraendert", activity_id=a.id,
                                       message=f"«{a.title}» ist fest/gebucht und darf nicht automatisch geändert werden"))
        return out

    def _constraints(self, before: Itinerary, after: Itinerary, k: TravelConstraints) -> list[Problem]:
        old = {a.id: a for d in before.days for a in d.activities}
        avoid, max_int = k.effective_avoid_tags(), k.effective_max_intensity()
        out = []
        for d in after.days:
            for a in d.activities:
                if old.get(a.id) == a:
                    continue  # nur Neues/Geändertes prüfen; bestehende Wahl bleibt Sache des Nutzers
                if a.intensity > max_int:
                    out.append(Problem(code="zu_anstrengend", activity_id=a.id,
                                       message=f"«{a.title}» ist anstrengender als erlaubt"))
                if a.tags & avoid:
                    out.append(Problem(code="ausgeschlossen", activity_id=a.id,
                                       message=f"«{a.title}» hat ausgeschlossene Merkmale: {', '.join(sorted(a.tags & avoid))}"))
        return out

    def _budget(self, before: Itinerary, after: Itinerary, k: TravelConstraints) -> list[Problem]:
        if k.budget_total_max is None:
            return []
        total = after.total_cost()
        if total > k.budget_total_max and total > before.total_cost():
            level = "warnung" if self.config.allow_budget_overrun else "fehler"
            return [Problem(code="budget", level=level,
                            message=f"Budget überschritten: {total} von {k.budget_total_max} {k.currency}")]
        return []
