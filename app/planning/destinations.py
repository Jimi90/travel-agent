"""Zielvorschläge: KI schlägt passende Ziele vor, der Code filtert hart und prüft das Klima.

Harte Regeln liegen im Code, nicht im Prompt: besuchte Länder, maximale Flugzeit und Umstiege
werden nach der KI-Antwort nochmals gefiltert. Das Klima kommt aus echten Messwerten
(gleiche Reisezeit im Vorjahr, Open-Meteo), nicht aus der KI.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel

from .. import config, llm, photos, weather
from ..models import Household
from ..models.common import new_id
from .matching import climate_score, match


ISO_CODES = frozenset("AD AE AF AG AI AL AM AO AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GT GU GW GY HK HN HR HT HU ID IE IL IM IN IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG US UY UZ VA VC VE VG VI VN VU WF WS XK YE YT ZA ZM ZW".split())


class DestinationIdea(BaseModel):
    name: str
    country: str  # ISO-3166 Alpha-2, z. B. «PT» (nicht der Landesname)
    country_name: str = ""  # Landesname auf Deutsch, z. B. «Portugal»
    region: str  # Region/Provinz/Insel, z. B. «Algarve», «Toskana», «Gran Canaria»
    landmark_wiki: str = ""  # Wikipedia-Titel eines ikonischen Wahrzeichens/einer Landschaft – das Bild der Karte
    lat: float
    lon: float
    why: str  # 1 Satz: warum es zu BEIDEN passt
    pitch: str = ""  # 2–3 Sätze im Reisebüro-Stil: was das Ziel auszeichnet
    climate_note: str = ""  # 1–2 Sätze: wie sich das Wetter zur Reisezeit anfühlt
    highlights: list[str]
    travel_mode: Literal["flug", "zug", "auto"]  # beste Anreise ab dem Heimatort
    travel_hours: float  # Reisezeit pro Strecke (Flug: reine Flugzeit)
    typical_stops: int  # Umstiege beim Flug, sonst 0
    water_temp_c: float = 0  # Meerwassertemperatur zur Reisezeit in °C, 0 wenn kein Badestrand/Meer am Ziel
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
Halte dich strikt an alle Randbedingungen, besonders an die Reichweite.
Pro Ziel: «country» = genau der zweibuchstabige ISO-Code des Landes (Mexiko = «MX», Senegal = «SN», Kap Verde = «CV»),
«country_name» = Landesname auf Deutsch, «region» = Region, Provinz oder Insel (z. B. «Algarve», «Toskana»), «highlights» = 3 bis 4 kurze, konkrete
Stichworte (je höchstens 3 Wörter, z. B. «Vulkan Poás», «Altstadt Alfama»), «landmark_wiki» = Titel des Wikipedia-Artikels
eines weltbekannten Wahrzeichens oder einer ikonischen Landschaft dieses Ziels, die man auf einem Foto sofort dem Ziel
zuordnet (z. B. «Torre de Belém», «Vulkan Arenal»). NICHT der Artikel über die Stadt oder das Land, kein Wappen, keine Flagge.
«water_temp_c» = übliche Meerwassertemperatur am Ziel zur Reisezeit in °C (0, wenn kein Meer/Badestrand).
Texte wie ein gutes Reisebüro – einladend, lebendig, konkret mit Namen, ohne Floskeln («ein Paradies», «für jeden etwas»):
«pitch» = 2 bis 3 Sätze, was dieses Ziel auszeichnet und wie es sich anfühlt, dort zu sein (Landschaft, Atmosphäre,
Essen, besondere Erlebnisse). «climate_note» = 1 bis 2 Sätze, wie das Wetter zur Reisezeit ist und sich anfühlt
(z. B. «Im Dezember angenehm warm, perfekt für den Strand; abends genügt eine leichte Jacke.»), ehrlich auch bei
Regen oder Kälte. «why» = 1 kurzer Satz, warum es zu euch beiden passt (Du-Form plural, «euch»)."""


def resolve_country(idea: DestinationIdea) -> str:
    """ISO-Code des Landes. Die KI liefert oft den Landesnamen statt des Codes («Mexiko» → «ME» = Montenegro);
    deshalb gegen die Ortssuche (Ort und Landesname) abgleichen. Leer, wenn nichts sicher ist."""
    raw = idea.country.strip().upper()
    iso = raw if raw in ISO_CODES else ""

    def code_of(q: str) -> str:
        q = (q or "").split("(")[0].strip()
        if not q:
            return ""
        try:
            return weather.geocode(q).country.upper()
        except Exception:  # Netz, nicht gefunden
            return ""

    if config.FAKE_MODE:
        return iso
    by_place = code_of(idea.name)
    by_name = code_of(idea.country_name or (idea.country if not iso else ""))
    if iso and iso in (by_place, by_name):
        return iso
    if by_name and by_name == by_place:
        return by_name
    # KI-Code und Ortssuche widersprechen sich: der Landesname ist verlässlicher als ein abgeschnittener Code
    return by_name or iso or by_place


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
        cc = resolve_country(idea)
        if cc and cc in m.excluded_countries:
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
        item = {**idea.model_dump(), "country": cc, "country_name": idea.country_name.strip(), "id": new_id(),
                "typical_flight_hours": idea.travel_hours}  # (alter Feldname für ältere Anzeige)
        try:
            clim = weather.climate(idea.lat, idea.lon, start, days)
            item["climate_score"], item["climate_text"] = climate_score(m, clim)
            item["climate"] = clim
        except weather.WeatherError as e:
            item["climate_score"], item["climate_text"] = None, f"Klimadaten nicht verfügbar ({e})"
        sea = None
        try:
            sea = weather.sea_temperature(idea.lat, idea.lon, start, days)
        except Exception:  # Zusatzinfo, darf den Vorschlag nie verhindern
            pass
        if sea is not None and sea > 5:
            item["sea_temp"], item["sea_source"] = round(sea), "gemessen"
        elif idea.water_temp_c and idea.water_temp_c > 5:
            item["sea_temp"], item["sea_source"] = round(idea.water_temp_c), "geschätzt"
        item["photo"] = photos.destination_photo(idea.landmark_wiki, idea.name, idea.region, idea.highlights)
        if m.budget_total and idea.est_total_chf > float(m.budget_total):
            item["warning"] = f"Geschätzt {idea.est_total_chf} CHF – über dem Budget"
        out.append(item)
    if m.budget_total:  # Budget hart: deutlich teurere Ziele fliegen raus (10 % Spielraum), ausser nichts bleibt übrig
        limit = float(m.budget_total) * 1.10
        fits = [i for i in out if i["est_total_chf"] <= limit]
        if fits:
            dropped += [f"{i['name']}: ca. CHF {i['est_total_chf']} – über dem Budget" for i in out if i not in fits]
            out = fits
    # zuerst, was ins Budget passt, dann nach Klima
    out.sort(key=lambda x: (bool(x.get("warning")), -(x["climate_score"] if x["climate_score"] is not None else 5)))
    return {"start": start.isoformat(), "days": days, "wish": wish, "ideas": out[:6], "dropped": dropped,
            "reach": m.reach.text(m.home), "budget": float(m.budget_total) if m.budget_total else None,
            "travel_budget": float(m.travel_budget) if m.travel_budget else None}


def _fake(h: Household) -> DestinationList:
    base = [("Lissabon", "PT", "Region Lissabon", "Torre de Belém", 38.72, -9.14, "flug", 2.8, 0, 380),
            ("Madeira (Funchal)", "PT", "Madeira", "Pico do Arieiro", 32.65, -16.91, "flug", 3.8, 0, 520),
            ("Kreta (Chania)", "GR", "Kreta", "Balos Lagoon", 35.51, 24.02, "flug", 2.9, 0, 420),
            ("Rom", "IT", "Latium", "Kolosseum", 41.9, 12.5, "flug", 1.5, 0, 220),
            ("Valencia", "ES", "Valencia", "Ciudad de las Artes y las Ciencias", 39.47, -0.38, "flug", 1.9, 0, 260),
            ("Malta (Valletta)", "MT", "Malta", "Valletta", 35.9, 14.51, "flug", 2.4, 0, 340),
            ("Lyon", "FR", "Auvergne-Rhône-Alpes", "Basilique Notre-Dame de Fourvière", 45.76, 4.84, "zug", 4.5, 0, 180),
            ("Doha", "QA", "Doha", "Museum of Islamic Art, Doha", 25.29, 51.53, "flug", 6.5, 0, 900),
            ("Colmar", "FR", "Elsass", "Petite Venise (Colmar)", 48.08, 7.36, "auto", 1.8, 0, 90)]
    return DestinationList(ideas=[DestinationIdea(
        name=n, country=c, country_name=c, region=rg, landmark_wiki=lm, lat=la, lon=lo,
        why=f"Passt zu euch: Kultur für James, Wellness und gutes Essen für beide.",
        pitch=f"{n} verbindet verwinkelte Gassen, lebendige Märkte und lange Abende am Wasser. Tagsüber entdeckt ihr "
              "Aussichtspunkte und kleine Museen, abends wartet frischer Fisch in einer Taverne am Hafen.",
        climate_note="Mild und meist sonnig, ideal zum Flanieren; abends wird es frisch, eine leichte Jacke genügt.",
        highlights=["Altstadt", "Küste", "Essen"], travel_mode=mode, travel_hours=hrs, typical_stops=st,
        travel_cost_chf=tc, cost_level="mittel", est_total_chf=1500 + i * 300)
        for i, (n, c, rg, lm, la, lo, mode, hrs, st, tc) in enumerate(base)])
