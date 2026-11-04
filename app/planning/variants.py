"""Routen-Varianten: 2–3 grobe Vorschläge für ein Ziel, bevor der ganze Plan entsteht.

Die KI liefert Route (Orte + Nächte), Stichpunkte und Kostenschätzung. Der Code ergänzt echte Daten:
Wetter aus Messwerten derselben Tage im Vorjahr (Open-Meteo), Distanzen zwischen den Orten, Fotos.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from pydantic import BaseModel

from .. import llm, photos, weather
from ..models import Household
from ..models.common import new_id
from .matching import HOME_COORDS, climate_score, match


class Stop(BaseModel):
    name: str
    country: str  # ISO-3166 Alpha-2
    lat: float
    lon: float
    nights: int


class Variant(BaseModel):
    name: str  # z. B. «Stadt & Strand»
    summary: str  # 1–2 Sätze
    route: list[Stop]
    bullets: list[str]  # 3–5 kurze Stichpunkte (Wasser, Tempo, Highlights, Anreise …)
    water_temp_c: float  # Meerwassertemperatur zur Reisezeit, 0 wenn kein Meer
    flights_chf: int  # Anreise hin und zurück (Flug/Zug/Auto) – Schätzungen für beide zusammen
    hotels_chf: int
    activities_chf: int
    transfers_chf: int


class VariantList(BaseModel):
    variants: list[Variant]


SYSTEM = """Du bist Kompass, ein Reiseplaner für ein Paar aus der Schweiz. Deutsch, Schweizer Rechtschreibung (kein ß).
Erstelle 3 deutlich verschiedene Routen-Varianten für das Ziel (z. B. ruhig an einem Ort, Rundreise, Stadt + Strand).
Jede Route: Orte in Reisereihenfolge mit Nächten; Summe der Nächte = Reisetage − 1. Nur echte Orte mit Koordinaten.
bullets: 3–5 kurze, konkrete Stichpunkte (z. B. Reisetempo, Highlights, Wege/Transfers, Besonderheiten zur Reisezeit).
Kosten ehrlich schätzen, in CHF für beide zusammen. Alle Randbedingungen strikt einhalten."""


def km(a: Stop, b: Stop) -> float:
    r = 6371.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def suggest(h: Household, destination: str, start: date, days: int, wishes: str, home_airport: str,
            travel_budget=None) -> dict:
    m = match(h, start, days, home_airport, travel_budget)
    prompt = (f"Ziel: {destination}\nReise: ab {start:%d.%m.%Y}, {days} Tage ({days - 1} Nächte). "
              f"Heimatort: {m.home} (Flughafen {home_airport}).\n{m.prompt_text()}\n"
              f"Höchstens {m.reach.max_bases} Aufenthaltsort(e) pro Route. Anreise so wählen, wie es für diese Dauer "
              f"sinnvoll ist (bei nahen Zielen Zug oder Auto). Wünsche: {wishes.strip() or 'keine'}")
    res = llm.ask("varianten", system=SYSTEM, content=prompt, schema=VariantList, max_tokens=8000,
                  fake=lambda: _fake(destination, days))
    out = []
    home = HOME_COORDS.get(home_airport.upper())
    for v in res.variants[:3]:
        route = [s for s in v.route if s.nights > 0] or v.route[:1]
        if len(route) > m.reach.max_bases:  # Kurztrip: nicht durch mehrere Orte hetzen
            extra = sum(s.nights for s in route[m.reach.max_bases:])
            route = route[:m.reach.max_bases]
            route[-1].nights += extra
        nights = sum(s.nights for s in route)
        if nights != days - 1 and route:  # Nächte auf die Reisedauer abgleichen (letzter Ort gleicht aus)
            route[-1].nights = max(1, route[-1].nights + (days - 1 - nights))
        bullets = list(v.bullets[:5])
        # Wetter: echte Messwerte am ersten (Haupt-)Ort, gleiche Tage im Vorjahr
        score = None
        try:
            main = max(route, key=lambda s: s.nights)
            clim = weather.climate(main.lat, main.lon, start, days)
            score, txt = climate_score(m, clim)
            bullets.insert(0, f"Wetter {main.name}: tagsüber Ø {clim['temp_max']:.0f} °C, nachts Ø "
                              f"{clim['temp_min']:.0f} °C, Regentage {clim['rain_days'] * 100:.0f} % (Vorjahr)")
        except (weather.WeatherError, ValueError):
            pass
        if v.water_temp_c and v.water_temp_c > 5:
            bullets.insert(1, f"Wasser ca. {v.water_temp_c:.0f} °C (Schätzung)")
        dist = sum(km(a, b) for a, b in zip(route, route[1:]))
        if len(route) > 1:
            bullets.append(f"Route ca. {dist:,.0f} km zwischen {len(route)} Orten".replace(",", "'"))
        warn = ""
        if home and route:  # Luftlinie ab Heimatflughafen grob in Flugstunden (ca. 750 km/h + 30 Min.)
            far = max(km(Stop(name="", country="CH", lat=home[0], lon=home[1], nights=0), s_) for s_ in route)
            est_h = far / 750 + 0.5
            if m.reach.flight_hours and est_h > m.reach.flight_hours * 1.25:
                warn = f"Weit für {days} Tage: ca. {est_h:.0f} Std. Flug pro Strecke"
        if not warn and m.travel_budget and v.flights_chf > float(m.travel_budget) * 1.2:
            warn = f"Hin- und Rückreise ca. CHF {v.flights_chf} – über dem Budget dafür (CHF {m.travel_budget:.0f})"
        total = v.flights_chf + v.hotels_chf + v.activities_chf + v.transfers_chf
        item = {"id": new_id(), "name": v.name, "summary": v.summary, "bullets": bullets,
                "route": [s.model_dump() for s in route], "climate_score": score,
                "costs": {"Hin- und Rückreise": v.flights_chf, "Unterkünfte": v.hotels_chf, "Programm": v.activities_chf,
                          "Transfers": v.transfers_chf, "Total": total},
                "over_budget": bool(m.budget_total and total > float(m.budget_total)),
                "budget": float(m.budget_total) if m.budget_total else None, "warning": warn,
                "photo": photos.wiki_photo(route[0].name, must_match=route[0].name) if route else None}
        out.append(item)
    return {"id": new_id(), "destination": destination, "start": start.isoformat(), "days": days, "wishes": wishes,
            "travel_budget": float(m.travel_budget) if m.travel_budget is not None else None, "variants": out}


def route_text(route: list[dict]) -> str:
    return " → ".join(f"{s['name']} ({s['nights']} Nächte)" for s in route)


def _fake(destination: str, days: int) -> VariantList:
    a = destination.split(",")[0].strip() or "Ziel"
    n = days - 1
    mk = lambda name, la, lo, k: Stop(name=name, country="PT", lat=la, lon=lo, nights=k)  # noqa: E731
    return VariantList(variants=[
        Variant(name="Gemütlich an einem Ort", summary=f"Alles von {a} aus, kurze Wege.",
                route=[mk(a, 38.72, -9.14, n)], bullets=["Ruhiges Tempo", "Tagesausflüge möglich"],
                water_temp_c=17, flights_chf=500, hotels_chf=160 * n, activities_chf=400, transfers_chf=60),
        Variant(name="Stadt & Insel", summary=f"Erst {a}, dann Erholung auf der Insel.",
                route=[mk(a, 38.72, -9.14, n // 2), mk(f"{a} Insel", 32.65, -16.91, n - n // 2)],
                bullets=["Abwechslungsreich", "Eine Fährfahrt"], water_temp_c=20, flights_chf=560,
                hotels_chf=150 * n, activities_chf=450, transfers_chf=140),
        Variant(name="Rundreise", summary="Drei Orte, viel zu sehen.",
                route=[mk(a, 38.72, -9.14, max(1, n // 3)), mk("Sintra", 38.80, -9.38, max(1, n // 3)),
                       mk("Porto", 41.15, -8.61, n - 2 * max(1, n // 3))],
                bullets=["Mehr Ortswechsel", "Zug zwischen den Orten"], water_temp_c=0, flights_chf=520,
                hotels_chf=140 * n, activities_chf=500, transfers_chf=90),
    ])
