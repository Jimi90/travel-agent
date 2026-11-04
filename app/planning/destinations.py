"""Zielvorschläge: KI schlägt passende Ziele vor, der Code filtert hart und prüft das Klima.

Harte Regeln liegen im Code, nicht im Prompt: besuchte Länder, maximale Flugzeit und Umstiege
werden nach der KI-Antwort nochmals gefiltert. Das Klima kommt aus echten Messwerten
(gleiche Reisezeit im Vorjahr, Open-Meteo), nicht aus der KI.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel

from .. import llm, photos, weather
from ..models import Household
from ..models.common import new_id
from .matching import climate_score, match


class DestinationIdea(BaseModel):
    name: str
    country: str  # ISO-3166 Alpha-2
    region: str
    lat: float
    lon: float
    why: str
    highlights: list[str]
    travel_mode: Literal["flug", "zug", "auto"]  # beste Anreise ab dem Heimatort
    travel_hours: float  # Reisezeit pro Strecke (Flug: reine Flugzeit)
    typical_stops: int  # Umstiege beim Flug, sonst 0
    travel_cost_chf: int  # Anreise hin und zurück für beide (Schätzung)
    cost_level: Literal["guenstig", "mittel", "teuer"]
    est_total_chf: int


class DestinationList(BaseModel):
    ideas: list[DestinationIdea]


SYSTEM = """Du bist Kompass, ein Reiseplaner für ein Paar aus der Schweiz. Schreibe Deutsch mit Schweizer
Rechtschreibung (kein ß). Schlage nur echte, erreichbare Ziele vor. Begründe knapp und konkret, warum das Ziel
zu BEIDEN passt. Wähle die sinnvollste Anreise (Flug, Zug oder Auto) und schätze die Reisezeit pro Strecke, beim Flug
die übliche Zahl Umstiege, und die Gesamtkosten für zwei Personen für genau diese Reisedauer (Anreise, Unterkunft,
Aktivitäten) in CHF ehrlich. Bei kurzen Reisen nahe Ziele – auch in der Schweiz und den Nachbarländern.
Halte dich strikt an alle Randbedingungen, besonders an die Reichweite."""


def suggest(h: Household, start: date, days: int, wish: str, home_airport: str, travel_budget=None) -> dict:
    m = match(h, start, days, home_airport, travel_budget)
    prompt = (f"Reisezeit: ab {start:%d.%m.%Y}, {days} Tage. Heimatort: {m.home} (Flughafen {home_airport}).\n"
              f"{m.prompt_text()}\n"
              f"Wunsch/Hinweis: {wish.strip() or 'keiner – überrasche uns, gerne abwechslungsreich'}\n"
              "Schlage 8 unterschiedliche Ziele vor, die innerhalb der Reichweite liegen. "
              "Koordinaten (lat/lon) des Hauptorts angeben.")
    res = llm.ask("zielvorschlaege", system=SYSTEM, content=prompt, schema=DestinationList,
                  fake=lambda: _fake(h), max_tokens=8000)
    out, dropped = [], []
    for idea in res.ideas:
        cc = idea.country.strip().upper()[:2]
        if cc in m.excluded_countries:
            dropped.append(f"{idea.name}: Land schon besucht")
            continue
        r = m.reach
        if idea.travel_mode == "flug":
            if r.flight_hours and idea.travel_hours > r.flight_hours:
                dropped.append(f"{idea.name}: {idea.travel_hours:g} Std. Flug – zu weit für {days} Tage")
                continue
            if idea.typical_stops > r.stops:
                dropped.append(f"{idea.name}: {idea.typical_stops} Umstieg(e) – zu viel für {days} Tage")
                continue
        elif idea.travel_hours > r.ground_hours:
            dropped.append(f"{idea.name}: {idea.travel_hours:g} Std. mit {'Zug' if idea.travel_mode == 'zug' else 'Auto'}"
                           f" – zu weit für {days} Tage")
            continue
        if m.travel_budget and idea.travel_cost_chf > float(m.travel_budget) * 1.2:
            dropped.append(f"{idea.name}: Hin- und Rückreise ca. CHF {idea.travel_cost_chf} – über dem Budget dafür "
                           f"(CHF {m.travel_budget:.0f})")
            continue
        item = {**idea.model_dump(), "country": cc, "id": new_id(),
                "typical_flight_hours": idea.travel_hours}  # (alter Feldname für ältere Anzeige)
        try:
            clim = weather.climate(idea.lat, idea.lon, start, days)
            item["climate_score"], item["climate_text"] = climate_score(m, clim)
        except weather.WeatherError as e:
            item["climate_score"], item["climate_text"] = None, f"Klimadaten nicht verfügbar ({e})"
        short = idea.name.split("(")[0].strip()
        item["photo"] = photos.wiki_photo(short, must_match=short)
        if m.budget_total and idea.est_total_chf > float(m.budget_total):
            item["warning"] = f"Geschätzt {idea.est_total_chf} CHF – über dem Budget"
        out.append(item)
    # zuerst, was ins Budget passt, dann nach Klima
    out.sort(key=lambda x: (bool(x.get("warning")), -(x["climate_score"] if x["climate_score"] is not None else 5)))
    return {"start": start.isoformat(), "days": days, "wish": wish, "ideas": out[:6], "dropped": dropped,
            "reach": m.reach.text(m.home), "budget": float(m.budget_total) if m.budget_total else None,
            "travel_budget": float(m.travel_budget) if m.travel_budget else None}


def _fake(h: Household) -> DestinationList:
    base = [("Lissabon", "PT", 38.72, -9.14, "flug", 2.8, 0, 380), ("Madeira (Funchal)", "PT", 32.65, -16.91, "flug", 3.8, 0, 520),
            ("Kreta (Chania)", "GR", 35.51, 24.02, "flug", 2.9, 0, 420), ("Rom", "IT", 41.9, 12.5, "flug", 1.5, 0, 220),
            ("Valencia", "ES", 39.47, -0.38, "flug", 1.9, 0, 260), ("Malta (Valletta)", "MT", 35.9, 14.51, "flug", 2.4, 0, 340),
            ("Lyon", "FR", 45.76, 4.84, "zug", 4.5, 0, 180), ("Doha", "QA", 25.29, 51.53, "flug", 6.5, 0, 900),
            ("Colmar", "FR", 48.08, 7.36, "auto", 1.8, 0, 90)]
    return DestinationList(ideas=[DestinationIdea(
        name=n, country=c, region="", lat=la, lon=lo, why=f"Testvorschlag {n}: mildes Klima, gute Mischung.",
        highlights=["Altstadt", "Küste", "Essen"], travel_mode=mode, travel_hours=hrs, typical_stops=st,
        travel_cost_chf=tc, cost_level="mittel", est_total_chf=1500 + i * 300)
        for i, (n, c, la, lo, mode, hrs, st, tc) in enumerate(base)])
