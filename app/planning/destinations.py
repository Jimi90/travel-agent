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
    typical_flight_hours: float
    typical_stops: int
    cost_level: Literal["guenstig", "mittel", "teuer"]
    est_total_chf: int


class DestinationList(BaseModel):
    ideas: list[DestinationIdea]


SYSTEM = """Du bist Kompass, ein Reiseplaner für ein Paar aus der Schweiz. Schreibe Deutsch mit Schweizer
Rechtschreibung (kein ß). Schlage nur echte, erreichbare Ziele vor. Begründe knapp und konkret, warum das Ziel
zu BEIDEN passt. Schätze Flugzeit ab dem Heimatflughafen, übliche Umstiege und Gesamtkosten für zwei Personen
(Flüge, Unterkunft, Aktivitäten) in CHF ehrlich. Halte dich strikt an alle Randbedingungen."""


def suggest(h: Household, start: date, days: int, wish: str, home_airport: str) -> dict:
    m = match(h, start)
    prompt = (f"Reisezeit: ab {start:%d.%m.%Y}, {days} Tage. Heimatflughafen: {home_airport}.\n"
              f"{m.prompt_text()}\n"
              f"Wunsch/Hinweis: {wish.strip() or 'keiner – überrasche uns, gerne abwechslungsreich'}\n"
              "Schlage 6 unterschiedliche Ziele vor (verschiedene Länder). Koordinaten (lat/lon) des Hauptorts angeben.")
    res = llm.ask("zielvorschlaege", system=SYSTEM, content=prompt, schema=DestinationList,
                  fake=lambda: _fake(h), max_tokens=8000)
    out, dropped = [], []
    for idea in res.ideas:
        cc = idea.country.strip().upper()[:2]
        if cc in m.excluded_countries:
            dropped.append(f"{idea.name}: Land schon besucht")
            continue
        if m.max_flight_hours and idea.typical_flight_hours > m.max_flight_hours:
            dropped.append(f"{idea.name}: Flug zu lang ({idea.typical_flight_hours:g} Std.)")
            continue
        if idea.typical_stops > m.max_stops:
            dropped.append(f"{idea.name}: zu viele Umstiege")
            continue
        item = {**idea.model_dump(), "country": cc, "id": new_id()}
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
    out.sort(key=lambda x: -(x["climate_score"] if x["climate_score"] is not None else 5))
    return {"start": start.isoformat(), "days": days, "wish": wish, "ideas": out, "dropped": dropped}


def _fake(h: Household) -> DestinationList:
    base = [("Lissabon", "PT", 38.72, -9.14), ("Madeira (Funchal)", "PT", 32.65, -16.91),
            ("Kreta (Chania)", "GR", 35.51, 24.02), ("Rom", "IT", 41.9, 12.5), ("Valencia", "ES", 39.47, -0.38),
            ("Malta (Valletta)", "MT", 35.9, 14.51)]
    return DestinationList(ideas=[DestinationIdea(
        name=n, country=c, region="", lat=la, lon=lo, why=f"Testvorschlag {n}: mildes Klima, gute Mischung.",
        highlights=["Altstadt", "Küste", "Essen"], typical_flight_hours=2.5 + i * 0.3, typical_stops=0,
        cost_level="mittel", est_total_chf=2500 + i * 300) for i, (n, c, la, lo) in enumerate(base)])
