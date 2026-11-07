"""Reiseplan erstellen: KI entwirft Tage, Orte, Hotels, Verbindungen und Schlechtwetter-Programm;
der Code baut daraus einen geprüften Reiseplan und setzt die Randbedingungen hart durch.

Ehrlich: Hotel- und Flugdaten sind Vorschläge/Schätzungen der KI. Verfügbarkeit und Preise prüft man
über die beigelegten Suchlinks (Booking.com, Google Flights); gebucht wird nichts automatisch.
"""
from __future__ import annotations

import urllib.parse
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ValidationError

from .. import llm, weather
from ..core.plausibility import PlausibilityChecker
from ..models import (
    Accommodation, Activity, AlternativeCandidate, DayPlan, Household, HotelSuggestion, Itinerary, Place,
    TransportSegment,
)
from ..models.common import ActivityCategory, Exposure, Intensity, new_id, to_min
from .matching import match

Cat = Literal["wandern", "natur", "strand", "wassersport", "kultur", "museum", "stadt", "kulinarik", "wellness",
              "entspannung", "shopping", "nachtleben"]
Exp = Literal["indoor", "outdoor", "gemischt"]
TAGS = ["hitze", "tauchen", "hoehe", "achterbahn", "alkohol", "rohkost", "lange_wege", "boot", "reservierung"]


class DraftActivity(BaseModel):
    title: str
    category: Cat
    exposure: Exp
    intensity: int  # 1 leicht, 2 mittel, 3 anstrengend
    start: str  # HH:MM
    end: str
    cost_chf: float  # für beide zusammen
    tags: list[str]
    note: str
    wikipedia: str  # Titel des passenden Wikipedia-Artikels (Ort, Sehenswürdigkeit, Gericht), sonst ""
    venue: str = ""  # genauer Name des Restaurants/Cafés/Bars bei Essen & Trinken, sonst ""


class DraftDay(BaseModel):
    day: int  # 1 = erster Reisetag
    base: str
    theme: str
    activities: list[DraftActivity]


class DraftBase(BaseModel):
    name: str
    country: str
    lat: float
    lon: float
    timezone: str
    nights: int


class DraftAlternative(BaseModel):
    base: str
    title: str
    category: Cat
    exposure: Exp
    intensity: int
    duration_min: int
    cost_chf: float
    tags: list[str]
    opens: str  # HH:MM oder ""
    closes: str


class DraftHotel(BaseModel):
    base: str
    name: str
    area: str
    price_per_night_chf: float
    why: str
    tags: list[str]


class DraftTransport(BaseModel):
    day: int
    mode: Literal["flug", "faehre", "zug", "bus", "auto"]
    from_place: str
    to_place: str
    from_code: str  # IATA-Code bei Flügen, sonst ""
    to_code: str
    depart: str  # HH:MM
    duration_h: float
    via: list[str]
    carrier: str
    est_price_chf: float  # für beide zusammen
    note: str


class PlanDraft(BaseModel):
    title: str
    summary: str
    bases: list[DraftBase]
    days: list[DraftDay]
    alternatives: list[DraftAlternative]
    hotels: list[DraftHotel]
    transports: list[DraftTransport]
    cost_note: str


SYSTEM = f"""Du bist Kompass, ein sorgfältiger Reiseplaner für ein Paar aus der Schweiz. Schreibe Deutsch mit
Schweizer Rechtschreibung (kein ß). Sprich das Paar immer mit «ihr/euch» an, nie mit «Sie». Plane realistisch und konkret mit echten, existierenden Orten.
Regeln:
- Pro Tag 4–6 Programmpunkte zwischen 07:00 und 23:00: Vormittag, Mittagessen, Nachmittag, Abendessen und wenn es
  passt ein Abendprogramm – ohne Überschneidungen, mit Pausen und Wegzeiten. Nie Tage mit nur einem Punkt (ausser
  An-/Abreise).
- theme: Tagesmotto in 3–6 Wörtern, z. B. «Maya-Ruinen von Tulum» oder «Strandtag auf Isla Mujeres».
- title: konkret mit Namen, nie allgemein. Nicht «Abendessen», sondern «Abendessen im Restaurant La Parrilla»; nicht
  «Strand», sondern «Playa Delfines». Essen mit echtem Lokal, Aktivität mit echtem Ort/Veranstalter.
- note: 2–3 Sätze, die den Punkt aussagekräftig machen: was man dort erlebt, warum es zu BEIDEN passt, dazu praktische
  Tipps (Anfahrt und Dauer, Eintritt, Reservation, beste Tageszeit, was mitnehmen). Keine Floskeln wie «Genuss der
  lokalen Küche».
- An- und Abreisetage leichter planen. Wechsel zwischen Orten als Transport mit Abfahrt/Dauer angeben.
- exposure ehrlich: outdoor = wetterabhängig, indoor = wetterfest, gemischt = teils.
- intensity: 1 leicht, 2 mittel, 3 anstrengend. Kosten in CHF für beide zusammen (Schätzung).
- wikipedia: genauer Titel eines bestehenden Wikipedia-Artikels über genau den Ort, die Sehenswürdigkeit oder die
  Landschaft dieses Programmpunkts (deutsch, sonst englisch), z. B. «Wiedervereinigungspalast», «Playa Delfines»,
  «Isla Mujeres». Bei Essen, Transfers, Hotel, Ruhe und Einkaufen immer "" (kein Gericht, keine Küche, kein Foto).
- Sehenswürdigkeiten, Museen, Touren und Ausflüge: nur solche, die bei Reisenden (z. B. TripAdvisor) sehr gut bewertet
  sind (mindestens 4.5 von 5), keine Touristenfallen. Kompass prüft das nach und ersetzt schwach bewertete Punkte.
- venue: bei Essen & Trinken der genaue Name eines echten Restaurants, Cafés oder einer Bar am Ort, so wie bei Google
  Maps (z. B. «Tamarind», «Manda de Laos»), sonst "". Beim Frühstück im Hotel "". NUR sehr gut bewertete Lokale: bei
  Google mindestens 4.5 Sterne und viele Bewertungen (die bekannten Lieblinge von Einheimischen und Reisenden), keine
  Touristenfallen. Im Zweifel ein bekanntes, sehr beliebtes Lokal statt eines unbekannten.
- tags nur aus: {", ".join(TAGS)}. «hitze» für Sauna, Hammam, heisse Quellen; «hoehe» über 2500 m.
- alternatives: pro Aufenthaltsort mindestens 5 wetterfeste Ersatz-Aktivitäten (60–240 Min.), passend zu den
  Vorlieben, für Regentage (z. B. Museum, Spa ohne Sauna, Kochkurs, Markthalle).
- BUDGET ist eine harte Grenze: Hotels (Preis × Nächte), Verbindungen und alle Programmpunkte zusammen dürfen das
  genannte Gesamtbudget nicht überschreiten. Lieber günstigere Hotels, einfachere Restaurants und kostenlose oder
  günstige Programmpunkte wählen, als das Budget zu sprengen. Das erste (am besten passende) Hotel je Ort muss im
  Budget liegen.
- hotels: pro Aufenthaltsort genau 3 echte Hotels in verschiedenen Preisklassen; tags enthält genau eines von
  «guenstig», «mittel», «gehoben». Das am besten passende (im Budget) zuerst.
- transports: Anreise am Tag 1 ab dem Heimatort, Rückreise am letzten Tag, dazu alle Ortswechsel
  (Fähre, Zug, Inlandflug). Bei nahen Zielen Zug oder Auto statt Flug. Umstiege in via.
  Zeiten und Preise sind Schätzungen – im note-Feld so kennzeichnen.
- bases: Reihenfolge der Aufenthaltsorte mit Nächten; Summe der Nächte = Reisetage − 1.
- Alle Randbedingungen strikt einhalten."""


def generate(h: Household, destination: str, start: date, days: int, wishes: str, home_airport: str,
             web_search: bool = False, trip_id: str | None = None,
             route: list[dict] | None = None, travel_budget=None) -> tuple[Itinerary, list[str]]:
    m = match(h, start, days, home_airport, travel_budget)
    prompt = (f"Ziel: {destination}\nReise: {start:%d.%m.%Y} bis {start + timedelta(days=days - 1):%d.%m.%Y} "
              f"({days} Tage, {days - 1} Nächte). Heimatflughafen: {home_airport}.\n{m.prompt_text()}\n"
              f"Wünsche: {wishes.strip() or 'keine'}")
    if route:
        prompt += ("\nGewählte Route – genau so einhalten (Orte, Reihenfolge, Nächte): "
                   + " → ".join(f"{s['name']} ({s['nights']} Nächte)" for s in route))
    if web_search:
        prompt += ("\nNutze die Websuche für aktuelle Empfehlungen (auch Trends aus Reiseblogs und Social Media), "
                   "Öffnungszeiten und Hotelpreise. Nur echte Orte.")
    draft = llm.ask("reiseplan", system=SYSTEM, content=prompt, schema=PlanDraft, trip_id=trip_id,
                    fake=lambda: fake_draft(destination, days, home_airport, route), max_tokens=32000,
                    web_search=web_search)
    itin, notes = build(draft, h, start, days, home_airport, m.travel_budget)
    if route:
        want = [s["name"].casefold() for s in route]
        got = [b.name.casefold() for b in draft.bases]
        if not all(any(w in g or g in w for g in got) for w in want):
            notes.append("Der Plan weicht von der gewählten Route ab – bitte prüfen.")
    return itin, notes


DAYTRIP_RULES = """
TAGESAUSFLUG – diese Regeln gehen allen anderen vor:
- Genau 1 Tag, KEINE Übernachtung: bases = genau ein Eintrag (das Ausflugsziel) mit nights 0; hotels = [].
- transports: genau zwei, beide day 1 – Hinfahrt am Morgen vom Startort zum Ziel und Rückfahrt am Abend vom Ziel
  zurück zum Startort (from_place/to_place = genau der Name des Startorts), mit dem genannten Verkehrsmittel,
  from_code/to_code "". Preis für beide hin bzw. zurück.
- Programm: 4–6 Punkte zwischen Ankunft und Rückfahrt (nie gleichzeitig mit einer Fahrt), mit Mittagessen und einer
  Kaffee-/Zvieri-Pause; nicht zu dicht, es ist ein Ausflug.
- alternatives: 3 wetterfeste Ersatz-Aktivitäten am Ziel.
- Budget: ein Tag für beide."""


def generate_daytrip(h: Household, origin: Place, idea: dict, day: date, wishes: str, home_airport: str,
                     trip_id: str | None = None) -> tuple[Itinerary, list[str]]:
    """Ein Tagesausflug ab dem aktuellen Standort: Hin- und Rückfahrt am selben Tag, keine Unterkunft."""
    m = match(h, day, 1, home_airport)
    mode = {"zug": "Zug", "bus": "Bus", "auto": "Auto", "faehre": "Schiff/Fähre"}.get(idea.get("travel_mode"), "Zug")
    prompt = (f"Tagesausflug am {day:%d.%m.%Y} ab {origin.name} (Startort = aktueller Standort, Koordinaten "
              f"{origin.lat:.3f}, {origin.lon:.3f}) nach {idea['name']}"
              + (f" ({idea['region']})" if idea.get("region") else "") + ".\n"
              f"Verkehrsmittel: {mode}, ca. {idea.get('travel_minutes', 60)} Minuten pro Weg.\n"
              f"Ideen fürs Programm: {'; '.join(idea.get('highlights') or [])}. "
              f"Wichtigste Sehenswürdigkeit: {idea.get('tripadvisor') or idea['name']}.\n"
              f"{m.prompt_text()}\nWünsche: {wishes.strip() or 'keine'}")
    draft = llm.ask("reiseplan", system=SYSTEM + DAYTRIP_RULES, content=prompt, schema=PlanDraft, trip_id=trip_id,
                    fake=lambda: fake_daytrip(origin, idea), max_tokens=12000)
    draft = draft.model_copy(update={"hotels": [], "bases": [b.model_copy(update={"nights": 0}) for b in draft.bases[:1]]})
    itin, notes = build(draft, h, day, 1, home_airport, origin=origin)
    if not itin.title.lower().startswith("tagesausflug"):
        itin = itin.model_copy(update={"title": f"Tagesausflug {idea['name']}"[:160]})
    return itin, notes


# --- Entwurf → geprüfter Reiseplan ------------------------------------------------------------

def _t(s: str) -> time | None:
    try:
        hh, mm = s.strip().split(":")[:2]
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def _iata(code: str) -> str | None:
    c = (code or "").strip().upper()
    return c if len(c) == 3 and c.isalpha() else None


def _money(v: float) -> Decimal:
    return Decimal(str(round(max(0.0, float(v or 0)), 2)))


def _int(v: int) -> Intensity:
    return Intensity(min(3, max(1, int(v or 1))))


def booking_url(hotel: str, place: str, check_in: date, check_out: date) -> str:
    q = urllib.parse.urlencode({"ss": f"{hotel}, {place}", "checkin": check_in.isoformat(),
                                "checkout": check_out.isoformat(), "group_adults": 2, "no_rooms": 1})
    return f"https://www.booking.com/searchresults.html?{q}"


def flights_url(frm: str, to: str, d: date) -> str:
    q = urllib.parse.quote(f"Flights from {frm} to {to} on {d.isoformat()} for 2 adults")
    return f"https://www.google.com/travel/flights?q={q}"


def build(draft: PlanDraft, h: Household, start: date, days: int, home_airport: str,
          travel_budget: Decimal | None = None, origin: Place | None = None) -> tuple[Itinerary, list[str]]:
    notes: list[str] = []
    k = h.constraints
    avoid, max_int = k.effective_avoid_tags(), k.effective_max_intensity()
    end = start + timedelta(days=days - 1)

    places: dict[str, Place] = {}
    for b in draft.bases:
        cc = b.country.strip().upper()[:2]
        try:
            places[b.name.casefold()] = Place(name=b.name, country=cc, lat=b.lat, lon=b.lon, tz=b.timezone or None)
        except ValidationError:
            places[b.name.casefold()] = weather.geocode(b.name)
    if not places:
        raise llm.LLMError("Der Plan enthält keinen Aufenthaltsort.")
    first = next(iter(places.values()))
    home = origin or Place(name=home_airport, country="CH")  # Tagesausflug: Start/Ziel = aktueller Standort

    def place_of(name: str) -> Place:
        key = name.casefold()
        if key in places:
            return places[key]
        if origin and (key == origin.name.casefold() or origin.name.casefold() in key):
            return origin
        for k2, p in places.items():
            if k2 in key or key in k2:
                return p
        if name.upper() == home_airport.upper() or "zürich" in key or "zurich" in key:
            return home
        return Place(name=name, country=first.country)

    # Tage
    by_day = {d.day: d for d in draft.days}
    plan_days, prev_base = [], first
    for i in range(days):
        dd = by_day.get(i + 1)
        base = place_of(dd.base) if dd else prev_base
        acts: list[Activity] = []
        for a in (dd.activities if dd else []):
            s, e = _t(a.start), _t(a.end)
            if not s or not e or e <= s:
                notes.append(f"Tag {i + 1}: «{a.title}» ohne gültige Zeit weggelassen")
                continue
            tags = {t.strip().lower() for t in a.tags if t.strip()}
            act = Activity(title=a.title[:160], category=ActivityCategory(a.category), exposure=Exposure(a.exposure),
                           intensity=_int(a.intensity), start=s, end=e, place=base, cost=_money(a.cost_chf),
                           tags=tags, note=(a.note or None), wiki=(a.wikipedia or "").strip()[:200] or None,
                           venue=(a.venue or "").strip()[:120] or None)
            if act.intensity > max_int or act.tags & avoid:
                notes.append(f"Tag {i + 1}: «{a.title}» entfernt (Randbedingung: "
                             f"{'zu anstrengend' if act.intensity > max_int else ', '.join(sorted(act.tags & avoid))})")
                continue
            if acts and to_min(act.start) < to_min(acts[-1].end):
                notes.append(f"Tag {i + 1}: «{a.title}» überschneidet sich – weggelassen")
                continue
            if act.start < time(7) or act.end > time(23):
                notes.append(f"Tag {i + 1}: «{a.title}» ausserhalb 07–23 Uhr – weggelassen")
                continue
            acts.append(act)
            acts.sort(key=lambda x: x.start)
        plan_days.append(DayPlan(date=start + timedelta(days=i), base=base, activities=acts,
                                 theme=((dd.theme or "").strip()[:160] or None) if dd else None))
        prev_base = base

    # Unterkünfte: Reihenfolge der Orte mit Nächten, erstes passendes Hotel
    stays, cursor = [], start
    for b in draft.bases:
        n = max(0, int(b.nights))
        if n == 0 or cursor >= end:
            continue
        out = min(cursor + timedelta(days=n), end)
        p = place_of(b.name)
        hotels = [x for x in draft.hotels if place_of(x.base).same_area(p)]
        if hotels:
            ht = hotels[0]
            nights = (out - cursor).days
            stays.append(Accommodation(name=ht.name, place=p, check_in=cursor, check_out=out,
                                       cost=_money(ht.price_per_night_chf * nights), confirmed=False,
                                       search_url=booking_url(ht.name, p.name, cursor, out), note=ht.why or None))
        else:
            stays.append(Accommodation(name=f"Unterkunft {p.name} (noch offen)", place=p, check_in=cursor,
                                       check_out=out, confirmed=False,
                                       search_url=booking_url("", p.name, cursor, out)))
        cursor = out
    if cursor < end:
        notes.append(f"Für die Nächte ab {cursor:%d.%m.} fehlt eine Unterkunft")

    suggestions = []
    for x in draft.hotels:
        p = place_of(x.base)
        st = next((s for s in stays if s.place.same_area(p)), None)
        suggestions.append(HotelSuggestion(
            base=p.name, name=x.name, area=x.area, price_per_night=_money(x.price_per_night_chf), why=x.why,
            tags={t.lower() for t in x.tags},
            search_url=booking_url(x.name, p.name, st.check_in, st.check_out) if st else None))

    # Verbindungen
    transports = []
    for t in draft.transports:
        dep_t = _t(t.depart) or time(9)
        day = start + timedelta(days=max(0, min(days - 1, t.day - 1)))
        dep = datetime.combine(day, dep_t)
        arr = dep + timedelta(hours=max(0.25, float(t.duration_h or 1)))
        url = None
        if t.mode == "flug":
            url = flights_url(t.from_code or t.from_place, t.to_code or t.to_place, day)
        transports.append(TransportSegment(
            mode=t.mode, origin=place_of(t.from_place), destination=place_of(t.to_place), depart=dep, arrive=arr,
            carrier=t.carrier or None, cost=_money(t.est_price_chf), via=[v for v in t.via if v],
            origin_code=_iata(t.from_code), destination_code=_iata(t.to_code),
            estimated=True, search_url=url, note=t.note or None))
    transports.sort(key=lambda x: x.depart)
    for a, b in zip(transports, transports[1:]):
        if b.depart < a.arrive:
            notes.append(f"Verbindung {b.origin.name} → {b.destination.name} überschneidet sich mit der vorherigen – bitte prüfen")

    alts = []
    for a in draft.alternatives:
        tags = {t.strip().lower() for t in a.tags if t.strip()}
        try:
            alts.append(AlternativeCandidate(
                title=a.title[:160], category=ActivityCategory(a.category), exposure=Exposure(a.exposure),
                intensity=_int(a.intensity), duration_min=min(720, max(15, int(a.duration_min or 60))),
                cost=_money(a.cost_chf), place=place_of(a.base), tags=tags, opens=_t(a.opens), closes=_t(a.closes),
                source="plan"))
        except ValidationError:
            continue

    itin = Itinerary(
        id=new_id(), title=draft.title[:160] or f"Reise {draft.bases[0].name}", start_date=start, end_date=end,
        days=plan_days, stays=stays, transports=transports, alternatives=alts, hotel_suggestions=suggestions,
        summary=draft.summary, home_airport=home_airport, cost_note=draft.cost_note, status="entwurf",
        travel_budget=travel_budget,
    )
    empty = itin.model_copy(update={"days": [DayPlan(date=d.date, base=d.base) for d in itin.days]})
    problems = PlausibilityChecker().check(empty, itin, k, datetime.combine(start, time(0)))
    # Budget nie als Fehler: ein Plan über dem Budget wird trotzdem gezeigt, mit Hinweis (unten) – nicht abgelehnt
    soft = ("transport_reihenfolge", "budget")
    errors = [p for p in problems if p.level == "fehler" and p.code not in soft]
    notes += [p.message for p in problems if p.level == "warnung" or p.code == "transport_reihenfolge"]
    if errors:
        raise llm.LLMError("Der Plan ist nicht stimmig: " + "; ".join(p.message for p in errors[:3]))
    budget = k.budget_for(days, travel_budget)
    travel_cost = sum((t.cost for t in itin.transports), Decimal("0"))
    if travel_budget and travel_cost > travel_budget:
        notes.append(f"Hin- und Rückreise und Verbindungen ca. {travel_cost:.0f} CHF – über dem Budget dafür "
                     f"({travel_budget:.0f} CHF)")
    if budget and itin.total_cost() > budget:
        notes.append(f"Geschätzte Kosten {itin.total_cost():.0f} CHF liegen über dem Budget für {days} Tage "
                     f"({budget:.0f} CHF)")
    return itin, notes


# --- Testmodus ------------------------------------------------------------------------------

def fake_daytrip(origin: Place, idea: dict) -> PlanDraft:
    n = idea["name"]
    acts = [DraftActivity(title=f"Altstadt-Rundgang {n}", category="stadt", exposure="outdoor", intensity=1,
                          start="09:30", end="11:30", cost_chf=0, tags=[], wikipedia=idea.get("tripadvisor") or n,
                          note="Gemütlicher Rundgang durch die Gassen, am Morgen noch ruhig."),
            DraftActivity(title=f"Mittagessen im Restaurant Seeblick {n}", category="kulinarik", exposure="indoor",
                          intensity=1, start="12:00", end="13:30", cost_chf=90, tags=["reservierung"], note="",
                          wikipedia="", venue=f"Seeblick {n}"),
            DraftActivity(title=f"Schifffahrt ab {n}", category="natur", exposure="outdoor", intensity=1,
                          start="14:00", end="16:00", cost_chf=60, tags=[], wikipedia="",
                          note="Rundfahrt auf dem See mit Blick auf die Berge."),
            DraftActivity(title="Zvieri im Café am Quai", category="kulinarik", exposure="indoor", intensity=1,
                          start="16:15", end="17:00", cost_chf=20, tags=[], note="", wikipedia="", venue="")]
    mode = idea.get("travel_mode") if idea.get("travel_mode") in ("zug", "bus", "auto", "faehre") else "zug"
    h = max(0.25, int(idea.get("travel_minutes") or 60) / 60)
    return PlanDraft(
        title=f"Tagesausflug {n}", summary=f"Ein entspannter Tag in {n}: Altstadt, Mittagessen am See und eine Schifffahrt.",
        bases=[DraftBase(name=n, country="CH", lat=idea.get("lat") or 47.0, lon=idea.get("lon") or 8.3,
                         timezone="Europe/Zurich", nights=0)],
        days=[DraftDay(day=1, base=n, theme=f"Ein Tag in {n}", activities=acts)],
        alternatives=[DraftAlternative(base=n, title=f"Museum {n}", category="museum", exposure="indoor", intensity=1,
                                       duration_min=120, cost_chf=30, tags=[], opens="10:00", closes="17:00")],
        hotels=[],
        transports=[DraftTransport(day=1, mode=mode, from_place=origin.name, to_place=n, from_code="", to_code="",
                                   depart=f"{max(5, int(9.25 - h)):02d}:{0:02d}", duration_h=h, via=[], carrier="SBB" if mode == "zug" else "",
                                   est_price_chf=40, note="Zeit geschätzt"),
                    DraftTransport(day=1, mode=mode, from_place=n, to_place=origin.name, from_code="", to_code="",
                                   depart="17:30", duration_h=h, via=[], carrier="SBB" if mode == "zug" else "",
                                   est_price_chf=40, note="Zeit geschätzt")],
        cost_note="Alle Preise sind Schätzungen.")


def fake_draft(destination: str, days: int, home_airport: str, route: list[dict] | None = None) -> PlanDraft:
    """Ab 5 Tagen zwei Orte mit Fähre dazwischen – so lässt sich ein Fähr-Ausfall testen. Mit Route: deren Orte."""
    a = destination.split(",")[0].strip() or "Ziel"
    if route and len(route) == 1:
        a = route[0]["name"]
    two = days >= 5 if not route else len(route) >= 2
    n1 = ((days - 1) // 2 if not route else route[0]["nights"]) if two else days - 1
    bases = [DraftBase(name=a, country="PT", lat=38.7, lon=-9.1, timezone="Europe/Lisbon", nights=n1)]
    if two:
        second = route[1]["name"] if route else f"{a} Insel"
        bases.append(DraftBase(name=second, country="PT", lat=32.6, lon=-16.9, timezone="Atlantic/Madeira",
                               nights=days - 1 - n1))
    ds = []
    for i in range(1, days + 1):
        b = bases[0].name if (not two or i <= n1) else bases[1].name  # (Testplan: höchstens 2 Orte)
        acts = [DraftActivity(title=f"Küstenwanderung {i}", category="wandern", exposure="outdoor", intensity=2,
                              start="09:00", end="12:00", cost_chf=0, tags=[],
                              note="Gut ausgeschilderter Küstenweg mit Aussicht aufs Meer, etwa drei Stunden. Früh starten, "
                              "Wasser und Sonnenschutz mitnehmen.", wikipedia=""),
                DraftActivity(title=f"Altstadt-Museum {i}", category="museum", exposure="indoor", intensity=1,
                              start="14:00", end="16:00", cost_chf=30, tags=[],
                              note="Kleines Museum in der Altstadt mit lokaler Geschichte. Eintritt ca. CHF 15 pro Person, "
                              "am Nachmittag ruhiger.", wikipedia=f"Museu {i}"),
                DraftActivity(title=f"Abendessen im Restaurant Tasca {i}", category="kulinarik", exposure="indoor",
                              intensity=1, start="19:00", end="21:00", cost_chf=120, tags=["reservierung"], note="",
                              wikipedia="", venue=f"Tasca {i}")]
        if i == 1 or i == days:
            acts = acts[2:]
        if two and i == n1 + 1:  # Fähre am Wechseltag
            acts = acts[1:]
        ds.append(DraftDay(day=i, base=b, theme=f"Küste und Altstadt {i}", activities=acts))
    alts = []
    for b in bases:
        alts += [DraftAlternative(base=b.name, title=f"Thermalbad & Sauna {b.name}", category="wellness",
                                  exposure="indoor", intensity=1, duration_min=180, cost_chf=90, tags=["hitze"],
                                  opens="", closes=""),
                 DraftAlternative(base=b.name, title=f"Spa-Massage {b.name}", category="wellness", exposure="indoor",
                                  intensity=1, duration_min=150, cost_chf=160, tags=["reservierung"], opens="", closes=""),
                 DraftAlternative(base=b.name, title=f"Kochkurs {b.name}", category="kulinarik", exposure="indoor",
                                  intensity=1, duration_min=180, cost_chf=140, tags=[], opens="", closes=""),
                 DraftAlternative(base=b.name, title=f"Kunstmuseum {b.name}", category="museum", exposure="indoor",
                                  intensity=1, duration_min=120, cost_chf=30, tags=[], opens="", closes="")]
    hotels = []
    for b in bases:
        hotels += [DraftHotel(base=b.name, name=f"Hotel {b.name} Centro", area="Zentrum", price_per_night_chf=160,
                              why="Zentral, ruhig, gutes Frühstück", tags=["mittel"]),
                   DraftHotel(base=b.name, name=f"Casa {b.name}", area="Altstadt", price_per_night_chf=95,
                              why="Kleines Gästehaus, sehr persönlich", tags=["guenstig"]),
                   DraftHotel(base=b.name, name=f"Palácio {b.name}", area="Am Wasser", price_per_night_chf=290,
                              why="Spa ohne Sauna, Meerblick", tags=["gehoben"])]
    tr = [DraftTransport(day=1, mode="flug", from_place="Zürich", to_place=a, from_code=home_airport, to_code="LIS",
                         depart="07:00", duration_h=3, via=[], carrier="Testair", est_price_chf=500, note="Schätzung")]
    if two:
        tr.append(DraftTransport(day=n1 + 1, mode="faehre", from_place=a, to_place=bases[1].name, from_code="",
                                 to_code="", depart="09:00", duration_h=3, via=[], carrier="Testfähre",
                                 est_price_chf=120, note="Schätzung"))
    tr.append(DraftTransport(day=days, mode="flug", from_place=bases[-1].name, to_place="Zürich", from_code="LIS",
                             to_code=home_airport, depart="11:00", duration_h=3, via=[], carrier="Testair",
                             est_price_chf=500, note="Schätzung"))
    return PlanDraft(title=f"{a} ({days} Tage)", summary=f"Testplan für {a}.", bases=bases, days=ds,
                     alternatives=alts, hotels=hotels, transports=tr, cost_note="Alle Preise sind Schätzungen.")
