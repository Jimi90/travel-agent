"""MatchingModule: berechnet aus beiden Profilen die gemeinsame Schnittmenge für die Planung.

Regeln (bewusst einfach und nachvollziehbar):
- Aktivitäten: Mittelwert beider Vorlieben; -2 einer Person = Veto (fällt ganz weg).
  Ein grosser Unterschied (z. B. +2 / -1) wird als «Kompromiss» markiert: kommt vor, aber selten.
- Klima: Überschneidung beider Temperaturbereiche; ohne Überschneidung die Mitte dazwischen.
  Regen: die strengere Toleranz gilt.
- Budget: die tiefere Grenze pro Nacht gilt; Gesamtbudget aus den Randbedingungen.
- Randbedingungen (Schwangerschaft, Intensität, ausgeschlossene Merkmale, besuchte Länder) gelten immer.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from ..models import Household
from ..models.common import CATEGORY_LABELS, ActivityCategory, Intensity, Model

RAIN_ORDER = {"gering": 0, "mittel": 1, "hoch": 2}
INTENSITY_TEXT = {Intensity.LEICHT: "leicht", Intensity.MITTEL: "mittel", Intensity.ANSTRENGEND: "anstrengend"}


class CategoryMatch(Model):
    category: ActivityCategory
    label: str
    prefs: list[int]
    score: float | None  # None = Veto
    kind: str  # "beide" | "kompromiss" | "veto" | "egal"


class MatchingResult(Model):
    names: list[str]
    categories: list[CategoryMatch]
    temp_min_c: float
    temp_max_c: float
    climate_note: str
    rain_tolerance: str
    budget_per_night: Decimal | None
    budget_total: Decimal | None
    currency: str
    max_intensity: Intensity
    avoid_tags: list[str]
    excluded_countries: list[str]
    pregnancy_week: int | None = None
    max_flight_hours: float | None = None
    max_stops: int = 1

    @property
    def liked(self) -> list[CategoryMatch]:
        return [c for c in self.categories if c.score is not None and c.score >= 1]

    @property
    def vetoed(self) -> list[CategoryMatch]:
        return [c for c in self.categories if c.score is None]

    def prompt_text(self) -> str:
        """Kompakte Beschreibung für die KI (ohne Namen, nur was für die Planung nötig ist)."""
        lines = ["Reisende: 2 Erwachsene."]
        fav = [f"{c.label} ({c.score:+.1f})" for c in self.categories if c.score is not None and c.score > 0]
        lines.append("Gemeinsame Vorlieben (−2 bis +2): " + (", ".join(fav) or "keine besonderen"))
        comp = [c.label for c in self.categories if c.kind == "kompromiss"]
        if comp:
            lines.append("Kompromisse (nur sparsam einplanen): " + ", ".join(comp))
        if self.vetoed:
            lines.append("Auf keinen Fall: " + ", ".join(c.label for c in self.vetoed))
        lines.append(f"Wunschklima tagsüber {self.temp_min_c:.0f}–{self.temp_max_c:.0f} °C, Regentoleranz {self.rain_tolerance}.")
        if self.budget_per_night:
            lines.append(f"Unterkunft höchstens {self.budget_per_night} {self.currency} pro Nacht.")
        if self.budget_total:
            lines.append(f"Gesamtbudget höchstens {self.budget_total} {self.currency} (Flüge, Unterkünfte, Aktivitäten).")
        lines.append(f"Körperliche Anstrengung höchstens: {INTENSITY_TEXT[self.max_intensity]}.")
        if self.pregnancy_week is not None:
            lines.append(f"Eine Reisende ist schwanger (ca. Woche {self.pregnancy_week}): keine Sauna/Hitzebäder, "
                         "kein Tauchen, keine grossen Höhen, keine extremen Anstrengungen, kurze Wege, Pausen einplanen.")
        if self.avoid_tags:
            lines.append("Ausgeschlossene Merkmale: " + ", ".join(self.avoid_tags))
        if self.max_flight_hours:
            lines.append(f"Flugzeit pro Strecke höchstens {self.max_flight_hours:g} Std., höchstens {self.max_stops} Umstieg(e).")
        if self.excluded_countries:
            lines.append("Schon besuchte Länder (nicht vorschlagen): " + ", ".join(self.excluded_countries))
        return "\n".join(lines)


def match(h: Household, on: date | None = None) -> MatchingResult:
    cats = []
    for cat in ActivityCategory:
        prefs = [p.preference(cat) for p in h.profiles]
        score = h.harmonized(cat)
        if score is None:
            kind = "veto"
        elif max(prefs) - min(prefs) >= 3:
            kind = "kompromiss"
        elif all(v >= 1 for v in prefs):
            kind = "beide"
        else:
            kind = "egal"
        cats.append(CategoryMatch(category=cat, label=CATEGORY_LABELS[cat], prefs=prefs, score=score, kind=kind))
    cats.sort(key=lambda c: (c.score is None, -(c.score or 0), c.label))

    lo = max(p.climate.temp_min_c for p in h.profiles)
    hi = min(p.climate.temp_max_c for p in h.profiles)
    note = "Beide Klimawünsche überschneiden sich."
    if lo > hi:
        mid = (lo + hi) / 2
        lo, hi = mid - 2, mid + 2
        note = "Die Klimawünsche überschneiden sich nicht – es gilt die Mitte dazwischen."
    rain = min((p.climate.rain_tolerance for p in h.profiles), key=RAIN_ORDER.__getitem__)
    nights = [p.budget.per_night_max for p in h.profiles if p.budget.per_night_max]
    k = h.constraints
    return MatchingResult(
        names=[p.name for p in h.profiles], categories=cats, temp_min_c=lo, temp_max_c=hi, climate_note=note,
        rain_tolerance=rain, budget_per_night=min(nights) if nights else None, budget_total=k.budget_total_max,
        currency=k.currency, max_intensity=k.effective_max_intensity(), avoid_tags=sorted(k.effective_avoid_tags()),
        excluded_countries=sorted(h.history.visited_countries),
        pregnancy_week=k.pregnancy.week_on(on or date.today()) if k.pregnancy else None,
        max_flight_hours=k.max_flight_hours, max_stops=k.max_stops,
    )


def climate_score(m: MatchingResult, clim: dict) -> tuple[float, str]:
    """0–10: wie gut das Klima (Vorjahr, gleiche Reisezeit) zum gemeinsamen Wunsch passt."""
    t = clim["temp_max"]
    dist = max(0.0, m.temp_min_c - t, t - m.temp_max_c)
    rain_limit = {"gering": 0.15, "mittel": 0.3, "hoch": 0.5}[m.rain_tolerance]
    rain_pen = max(0.0, clim["rain_days"] - rain_limit) * 10
    score = max(0.0, 10 - dist * 1.2 - rain_pen)
    txt = (f"Vorjahr: tagsüber Ø {t:.0f} °C, nachts Ø {clim['temp_min']:.0f} °C, "
           f"Regentage {clim['rain_days'] * 100:.0f} %")
    return round(score, 1), txt
