"""Tagesausflüge ohne Übernachtung ab dem aktuellen Standort (Wunsch James 1.17.0).

Ablauf: Standort kommt aus dem Browser (GPS, `weather.reverse` → Ortsname) → TripAdvisor nennt beliebte Orte und Touren
in der Umgebung (1 SerpApi-Suche, Anregung für die KI) → die KI schlägt 6 Ausflugsziele vor (Anfahrt, Programm,
Sehenswürdigkeit wie bei TripAdvisor) → der Code prüft hart: Luftlinie und Fahrzeit passen zur gewählten Grenze,
TripAdvisor-Bewertung der Haupt-Sehenswürdigkeit (schwach = < 4.0 fliegt raus), Wetter genau für den Tag (Prognose
16 Tage), Foto. Ausgewählt → `generator.generate_daytrip` plant den Tag (Hin- und Rückfahrt, keine Unterkunft).
"""
from __future__ import annotations

from datetime import date
from math import asin, cos, radians, sin, sqrt
from typing import Literal

from pydantic import BaseModel

from .. import llm, photos, sights, weather
from ..models import Household, Place, new_id
from .matching import match

MODES = {"egal": "Zug, Bus oder Auto – was besser passt", "oev": "nur öffentlicher Verkehr (Zug, Bus, Schiff)",
         "auto": "mit dem Auto"}
MAX_KMH = 90  # keine Fahrt am Boden schafft mehr Luftlinie pro Stunde (auch Schnellzug mit Umsteigen nicht)
WEAK = 4.0


class DayTripIdea(BaseModel):
    name: str  # Ziel, z. B. «Luzern»
    country: str  # ISO-2
    region: str
    lat: float
    lon: float
    travel_mode: Literal["zug", "auto", "bus", "faehre"]
    travel_minutes: int  # einfacher Weg, Tür zu Tür
    pitch: str  # 2 Sätze wie im Reisebüro
    highlights: list[str]  # 3 Programmpunkte
    tripadvisor: str  # Name der wichtigsten Sehenswürdigkeit, wie bei TripAdvisor
    wikipedia: str  # Wikipedia-Titel eines Wahrzeichens (für das Foto)
    cost_chf: float  # Tag für beide inkl. Fahrt, Eintritte, Essen
    why: str  # warum es zu beiden passt


class DayTripList(BaseModel):
    ideas: list[DayTripIdea]


SYSTEM = """Du bist Kompass, ein Reiseplaner für ein Paar aus der Schweiz. Deutsch, Schweizer Rechtschreibung (kein ß),
«ihr/euch», nie «Sie». Du schlägst TAGESAUSFLÜGE vor: Hinfahrt am Morgen, Rückkehr am Abend zum Startort, KEINE
Übernachtung. Nur echte, existierende Orte mit genauen Koordinaten.
Regeln:
- travel_minutes = realistische Fahrzeit für EINEN Weg, Tür zu Tür (inkl. Umsteigen/Parkieren). Halte die Grenze strikt ein.
- Abwechslung: Natur, Kultur, Stadt, Wasser – nicht 6× dasselbe. Nicht der Startort selbst.
- pitch: 2 Sätze wie ein gutes Reisebüro, konkret (was erlebt man), keine Floskeln.
- highlights: 3 konkrete Programmpunkte mit Namen (je 3–8 Wörter), z. B. «Schifffahrt nach Vitznau».
- tripadvisor: der genaue Name der wichtigsten Sehenswürdigkeit dort, so wie er bei TripAdvisor heisst (z. B.
  «Rigi Kulm», «Rheinfall»). Nur Ziele, die bei Reisenden sehr gut bewertet sind (mindestens 4.5 von 5).
- wikipedia: Titel eines Wikipedia-Artikels über ein Wahrzeichen/eine Landschaft dort (für ein schönes Bild).
- cost_chf: ganzer Tag für beide (Fahrt, Eintritte, Essen), Schätzung.
- Wenn TripAdvisor-Lieblinge aus der Umgebung genannt werden: nutze sie als Anregung, wenn sie passen."""


def km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    p1, p2 = radians(a_lat), radians(b_lat)
    d = sin((p2 - p1) / 2) ** 2 + cos(p1) * cos(p2) * sin(radians(b_lon - a_lon) / 2) ** 2
    return 2 * 6371 * asin(sqrt(d))


def suggest(h: Household, origin: Place, day: date, max_minutes: int, mode: str, wish: str,
            home_airport: str = "ZRH") -> dict:
    m = match(h, day, 1, home_airport)
    fans = sights.nearby(f"Ausflüge {origin.name}", origin.lat, origin.lon)
    prompt = (f"Startort (aktueller Standort): {origin.name}"
              + (f" ({origin.country})" if origin.country != "XX" else "")
              + f", Koordinaten {origin.lat:.3f}, {origin.lon:.3f}.\n"
              f"Datum: {day:%d.%m.%Y} ({['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag'][day.weekday()]}).\n"
              f"Anreise: {MODES.get(mode, MODES['egal'])}, höchstens {max_minutes} Minuten pro Weg.\n"
              f"{m.prompt_text()}\n"
              f"Wunsch: {wish.strip() or 'keiner – überrascht uns'}\n")
    if fans:
        prompt += ("TripAdvisor-Lieblinge in der Umgebung: "
                   + "; ".join(f"{f['title']} ★ {f['rating']:.1f}" + (f" ({f['location']})" if f.get("location") else "")
                               for f in fans[:10]) + "\n")
    prompt += "Schlage 6 unterschiedliche Tagesausflüge vor, die innerhalb der Fahrzeit liegen."
    res = llm.ask("ausfluege", system=SYSTEM, content=prompt, schema=DayTripList, max_tokens=6000,
                  fake=lambda: _fake(origin))
    out, dropped = [], []
    for idea in res.ideas:
        dist = km(origin.lat, origin.lon, idea.lat, idea.lon)
        if mode == "oev" and idea.travel_mode == "auto":
            dropped.append(f"{idea.name}: nur mit dem Auto")
            continue
        if idea.travel_minutes > max_minutes * 1.15 or dist > max_minutes / 60 * MAX_KMH:
            dropped.append(f"{idea.name}: zu weit ({idea.travel_minutes} Min., {dist:.0f} km Luftlinie)")
            continue
        if dist < 3:
            dropped.append(f"{idea.name}: liegt am Startort")
            continue
        item = {**idea.model_dump(), "id": new_id(), "km": round(dist)}
        ta = sights.lookup(idea.tripadvisor or idea.name, idea.name)
        if ta and ta.get("found"):
            if ta.get("rating") is not None and ta["rating"] < WEAK and (ta.get("reviews") or 0) >= sights.WEAK_MIN_REVIEWS:
                dropped.append(f"{idea.name}: {ta.get('title')} bei TripAdvisor nur ★ {ta['rating']:.1f}")
                continue
            item["ta"] = {"name": ta.get("title"), "rating": ta.get("rating"), "reviews": ta.get("reviews"),
                          "url": ta.get("link") or sights.search_url(ta.get("title") or idea.name, idea.name),
                          "key": [idea.tripadvisor or idea.name, idea.name]}
        try:
            cc = idea.country.strip().upper()
            place = Place(name=idea.name[:120], country=cc if len(cc) == 2 and cc.isalpha() else "XX",
                          lat=idea.lat, lon=idea.lon)
            f = weather.lookup(place, day)
        except ValueError:
            f = None
        if f:
            item["weather"] = {"summary": f.summary, "tmax": f.temp_max_c, "tmin": f.temp_min_c,
                               "p": f.precip_probability, "mm": f.precip_mm, "storm": f.thunderstorm}
        item["photo"] = photos.destination_photo(idea.wikipedia, idea.name, idea.region, idea.highlights)
        out.append(item)
    # Tipps aus den TripAdvisor-Bewertungen (eine kleine KI-Aufgabe für alle)
    todo = []
    for it in out:
        if it.get("ta"):
            n, p = it["ta"]["key"]
            c = sights.cached(n, p) or {}
            if c.get("quote") and "tip" not in c:
                todo.append((n, p, c))
    if todo:
        sights.tips_for(todo)
    for it in out:
        if it.get("ta"):
            it["ta"]["tip"] = (sights.cached(*it["ta"]["key"]) or {}).get("tip", "")

    def rank(i):
        w = i.get("weather") or {}
        wet = 1 if (w.get("p") or 0) >= 60 or w.get("storm") else 0
        return (wet, -((i.get("ta") or {}).get("rating") or 4.2), i["travel_minutes"])
    out.sort(key=rank)
    return {"id": new_id(), "origin": origin.model_dump(), "date": day.isoformat(), "max_minutes": max_minutes,
            "mode": mode, "wish": wish, "ideas": out, "dropped": dropped,
            "tripadvisor": [f["title"] for f in fans[:6]]}


def _fake(origin: Place) -> DayTripList:
    base = [("Luzern", 47.05, 8.31, "zug", 50, "Kapellbrücke"), ("Rheinfall", 47.68, 8.62, "zug", 45, "Rheinfall"),
            ("Rigi", 47.06, 8.49, "zug", 75, "Rigi Kulm"), ("Bern", 46.95, 7.45, "zug", 60, "Historisches Museum 2"),
            ("Mailand", 45.46, 9.19, "zug", 200, "Mailänder Dom"), ("Altstadt Museum 2", 47.37, 8.54, "auto", 10, "Museu 2")]
    return DayTripList(ideas=[DayTripIdea(
        name=n, country="CH" if lat > 46 else "IT", region="Testregion", lat=lat, lon=lon, travel_mode=mode,
        travel_minutes=mins, pitch=f"{n} verbindet Altstadt, See und Berge. Ideal für einen entspannten Tag zu zweit.",
        highlights=[f"Spaziergang durch {n}", "Schifffahrt auf dem See", "Zvieri mit Aussicht"], tripadvisor=ta,
        wikipedia=ta, cost_chf=180, why="Kurze Anreise, viel Abwechslung, passt zu euren Vorlieben.")
        for n, lat, lon, mode, mins, ta in base])
