"""Routen-Varianten: 2–3 grobe Vorschläge für ein Ziel, bevor der ganze Plan entsteht.

Die KI liefert Route (Orte + Nächte), Stichpunkte und Kostenschätzung. Der Code ergänzt echte Daten:
Wetter aus Messwerten derselben Tage im Vorjahr (Open-Meteo), Distanzen zwischen den Orten, Fotos.
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta

from pydantic import BaseModel

from .. import config, llm, photos, weather
from ..models import Household
from ..models.common import new_id
from .matching import HOME_COORDS, climate_score, match


class Stop(BaseModel):
    name: str
    country: str  # ISO-3166 Alpha-2
    lat: float
    lon: float
    nights: int
    note: str = ""  # 3–6 Wörter, was man hier macht/sieht (z. B. «Altstadt, Goldmuseum»)


class Variant(BaseModel):
    name: str  # z. B. «Stadt & Strand»
    summary: str  # 1–2 einladende Sätze wie im Reisekatalog
    route: list[Stop]
    bullets: list[str]  # 3 Erlebnisse im Reisebüro-Stil (je 6–14 Wörter) – keine Kosten/Wetter/Distanzen
    water_temp_c: float  # Meerwassertemperatur zur Reisezeit, 0 wenn kein Meer
    flights_chf: int  # Anreise hin und zurück (Flug/Zug/Auto) – Schätzungen für beide zusammen
    hotels_chf: int
    activities_chf: int
    transfers_chf: int


class VariantList(BaseModel):
    variants: list[Variant]


SYSTEM = """Du bist Kompass, ein Reiseplaner für ein Paar aus der Schweiz. Deutsch, Schweizer Rechtschreibung (kein ß). Sprich das Paar immer mit «ihr/euch» an, nie mit «Sie».
Erstelle 3 deutlich verschiedene Routen-Varianten für das Ziel (z. B. ruhig an einem Ort, Rundreise, Stadt + Strand).
Jede Route: Orte in Reisereihenfolge mit Nächten; Summe der Nächte = Reisetage − 1. Nur echte Orte mit Koordinaten.
Pro Ort: «note» = 3 bis 6 Wörter, was man dort sieht oder macht (z. B. «Altstadt, Goldmuseum»).
summary: 1–2 einladende Sätze wie in einem guten Reisekatalog: Stimmung und Charakter der Route, konkret statt Floskeln.
bullets: genau 3 Erlebnisse dieser Route, so geschrieben, wie ein gutes Reisebüro sie anpreist: lebendig, sinnlich,
konkret mit Namen, je 6 bis 14 Wörter, ganze Ausdrücke statt Schlagworte. Gut: «Sonnenaufgang über dem Toten Meer
in Ein Gedi», «Abends Hummus und Live-Musik in den Gassen von Jaffa», «Drei Orte, nur zwei Hotelwechsel – viel Zeit
zum Ankommen». Schlecht: «Mischung Kultur/Natur», «Zugverbindungen genutzt», «Weniger Hotelwechsel».
KEINE Kosten, Wetter, Wassertemperatur oder Distanzen in bullets und notes – die zeigt die App getrennt.
Alle Orte liegen im genannten Ziel (Land/Region) – keine Orte in anderen Ländern, ausser das Ziel nennt sie.
name beschreibt den Stil der Route und passt zu ihren Orten (z. B. «Gemütlich in Valletta», «Inseln & Altstadt»).
Kosten ehrlich schätzen, in CHF für beide zusammen. Alle Randbedingungen strikt einhalten.
BUDGET ist eine harte Grenze: Die Summe aus flights_chf + hotels_chf + activities_chf + transfers_chf darf das genannte
Gesamtbudget nicht überschreiten. Wähle dafür günstigere Orte, einfachere Unterkünfte, weniger Ortswechsel und
kostenlose Programmpunkte. Mindestens eine Variante soll klar unter dem Budget liegen."""


BUDGET_SLACK = 1.10
MAX_KM_COUNTRY = 3000  # Ziel «Name, CC»: Ort muss in CC liegen und höchstens so weit vom Zielort weg sein
MAX_KM_REGION = 1500  # frei eingegebenes Ziel: gleiches Land wie der gefundene Zielort oder in diesem Umkreis


def km(a: Stop, b: Stop) -> float:
    r = 6371.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _locate(stop: Stop) -> None:
    """Koordinaten der KI sind oft falsch (Orte vertauscht, gleichnamiger Ort im falschen Land): per Ortssuche im
    angegebenen Land überschreiben. Findet die Suche nichts, bleiben die Werte der KI."""
    if config.FAKE_MODE:
        return
    base = re.sub(r"\s*\(.*?\)", "", stop.name).strip()
    tries = [base] + ([base.rsplit(" de ", 1)[1].strip()] if " de " in base else [])
    for name in dict.fromkeys(t for t in tries if t):
        try:
            place = weather.geocode(name, stop.country)
        except (weather.WeatherError, ValueError, KeyError):
            continue
        if place.lat is not None and place.lon is not None:
            stop.lat, stop.lon = place.lat, place.lon
            return


def target(destination: str, geo=None):
    """Ländercode («Malta, MT» → MT) und Mittelpunkt des Ziels (Ortssuche), soweit bestimmbar."""
    geo = geo or weather.geocode
    m = re.search(r",\s*([A-Za-z]{2})\s*$", destination)
    cc = m.group(1).upper() if m else None
    name = destination[:m.start()].strip() if m else destination.strip()
    try:
        center = geo(name, cc)
    except (weather.WeatherError, ValueError, KeyError):
        center = None
    return cc, center


def inside(stop: Stop, cc: str | None, center) -> bool:
    """Liegt der Ort im gewählten Ziel? (James: Malta-Route mit «Malediven-Entspannung» und einem Ort in Pakistan.)
    Nach `_locate`: Koordinaten stammen dann aus der Ortssuche, sonst von der KI – darum auch die Distanz prüfen."""
    dist = km(Stop(name="", country="", lat=center.lat, lon=center.lon, nights=0), stop) \
        if center is not None and center.lat is not None and center.lon is not None else 0.0
    if cc:
        return stop.country.upper() == cc and dist <= MAX_KM_COUNTRY
    return center is None or stop.country.upper() == (center.country or "").upper() or dist <= MAX_KM_REGION


def suggest(h: Household, destination: str, start: date, days: int, wishes: str, home_airport: str,
            travel_budget=None) -> dict:
    m = match(h, start, days, home_airport, travel_budget)
    prompt = (f"Ziel: {destination}\nReise: ab {start:%d.%m.%Y}, {days} Tage ({days - 1} Nächte). "
              f"Heimatort: {m.home} (Flughafen {home_airport}).\n{m.prompt_text()}\n"
              f"Höchstens {m.reach.max_bases} Aufenthaltsort(e) pro Route. Anreise so wählen, wie es für diese Dauer "
              f"sinnvoll ist (bei nahen Zielen Zug oder Auto). Wünsche: {wishes.strip() or 'keine'}")
    check = not config.FAKE_MODE  # Testmodus: Testrouten haben feste Koordinaten
    cc, center = target(destination) if check else (None, None)
    good: list[tuple[Variant, list[Stop]]] = []
    outside: list[str] = []
    for _attempt in range(2):  # passt keine Route zum Ziel: einmal mit Hinweis nachfragen
        res = llm.ask("varianten", system=SYSTEM, content=prompt, schema=VariantList, max_tokens=8000,
                      fake=lambda: _fake(destination, days))
        good, outside = [], []
        for v in res.variants[:3]:
            route = [s for s in v.route if s.nights > 0] or v.route[:1]
            for s_ in route:
                _locate(s_)
            if check:
                outside += [s_.name for s_ in route if not inside(s_, cc, center)]
                route = [s_ for s_ in route if inside(s_, cc, center)]
            if route:
                good.append((v, route))
        if good:
            break
        prompt += (f"\nACHTUNG: Die letzte Antwort enthielt nur Orte ausserhalb von {destination} "
                   f"({', '.join(outside[:6])}). Nur Orte in {destination}!")
    if not good:
        raise llm.LLMError(f"Die KI hat keine Route in {destination} geliefert (vorgeschlagen: "
                           f"{', '.join(outside[:6]) or 'nichts'}). Bitte nochmals versuchen.")
    out = []
    home = HOME_COORDS.get(home_airport.upper())
    for v, route in good:
        if len(route) > m.reach.max_bases:  # Kurztrip: nicht durch mehrere Orte hetzen
            extra = sum(s.nights for s in route[m.reach.max_bases:])
            route = route[:m.reach.max_bases]
            route[-1].nights += extra
        nights = sum(s.nights for s in route)
        if nights != days - 1 and route:  # Nächte auf die Reisedauer abgleichen (letzter Ort gleicht aus)
            route[-1].nights = max(1, route[-1].nights + (days - 1 - nights))
        bullets = [b.strip() for b in v.bullets if b.strip()][:3]
        # Wetter: echte Messwerte am ersten (Haupt-)Ort, gleiche Tage im Vorjahr
        score, facts = None, {"weather": None, "water_c": None, "km": None, "stops": len(route)}
        try:
            main = max(route, key=lambda s: s.nights)
            clim = weather.climate(main.lat, main.lon, start, days)
            score, txt = climate_score(m, clim)
            facts["weather"] = {"place": main.name, "max": round(clim["temp_max"]), "min": round(clim["temp_min"]),
                                "rain_pct": round(clim["rain_days"] * 100), "score": score}
        except (weather.WeatherError, ValueError):
            pass
        if v.water_temp_c and v.water_temp_c > 5:
            facts["water_c"] = round(v.water_temp_c)
        dist = sum(km(a, b) for a, b in zip(route, route[1:]))
        if len(route) > 1:
            facts["km"] = round(dist)
        warn = ""
        if home and route:  # Luftlinie ab Heimatflughafen grob in Flugstunden (ca. 750 km/h + 30 Min.)
            far = max(km(Stop(name="", country="CH", lat=home[0], lon=home[1], nights=0), s_) for s_ in route)
            est_h = far / 750 + 0.5
            if m.reach.flight_hours and est_h > m.reach.flight_hours * 1.25:
                warn = f"Weit für {days} Tage: ca. {est_h:.0f} Std. Flug pro Strecke"
        if not warn and m.travel_budget and v.flights_chf > float(m.travel_budget) * 1.2:
            warn = f"Hin- und Rückreise ca. CHF {v.flights_chf} – über dem Budget dafür (CHF {m.travel_budget:.0f})"
        total = v.flights_chf + v.hotels_chf + v.activities_chf + v.transfers_chf
        item = {"id": new_id(), "name": v.name, "summary": v.summary, "bullets": bullets, "facts": facts,
                "route": [s.model_dump() for s in route], "climate_score": score,
                "costs": {"Hin- und Rückreise": v.flights_chf, "Unterkünfte": v.hotels_chf, "Programm": v.activities_chf,
                          "Transfers": v.transfers_chf, "Total": total},
                "over_budget": bool(m.budget_total and total > float(m.budget_total)),
                "budget": float(m.budget_total) if m.budget_total else None, "warning": warn,
                "photo": photos.place_photo(route[0].name, destination) if route else None}
        out.append(item)
    dropped = []
    if m.budget_total:  # Budget hart: Routen deutlich darüber fliegen raus (10 % Spielraum für Schätzfehler)
        limit = float(m.budget_total) * BUDGET_SLACK
        fits = [o for o in out if o["costs"]["Total"] <= limit]
        if fits:
            dropped = [f"{o['name']}: ca. CHF {o['costs']['Total']:.0f} – über dem Budget" for o in out if o not in fits]
            out = fits
        elif out:  # nichts passt: nur die günstigste zeigen, deutlich gekennzeichnet
            cheapest = min(out, key=lambda o: o["costs"]["Total"])
            cheapest["warning"] = (f"Keine Route im Budget (CHF {float(m.budget_total):.0f}) – das ist die günstigste. "
                                   "Mehr Budget, weniger Tage oder ein günstigeres Ziel wählen.")
            dropped = [f"{o['name']}: ca. CHF {o['costs']['Total']:.0f}" for o in out if o is not cheapest]
            out = [cheapest]
    return {"id": new_id(), "destination": destination, "start": start.isoformat(), "days": days, "wishes": wishes,
            "dropped": dropped, "outside": sorted(set(outside)),
            "travel_budget": float(m.travel_budget) if m.travel_budget is not None else None, "variants": out}


def route_text(route: list[dict]) -> str:
    return " → ".join(f"{s['name']} ({s['nights']} Nächte)" for s in route)


def _fake(destination: str, days: int) -> VariantList:
    a = destination.split(",")[0].strip() or "Ziel"
    n = days - 1
    mk = lambda name, la, lo, k: Stop(name=name, country="PT", lat=la, lon=lo, nights=k,  # noqa: E731
                                      note="Altstadt, Aussichtspunkte, Essen")
    return VariantList(variants=[
        Variant(name="Gemütlich an einem Ort", summary=f"Alles von {a} aus, kurze Wege.",
                route=[mk(a, 38.72, -9.14, n)], bullets=["Morgens Kaffee in der Altstadt, abends Fado in der Alfama",
                                                         "Tagesausflüge nach Sintra und an die Küste von Cascais"],
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
