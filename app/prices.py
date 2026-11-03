"""Echte Preise über SerpApi (Google Flights / Google Hotels). Gratis-Tarif: 250 Suchen pro Monat (Stand 2025).

Kompass zeigt Angebote an; übernommen wird nur, was man antippt – gebucht wird nie.
Antworten werden 6 Std. gemerkt (spart Kontingent). Jede echte Suche wird gezählt (Profil → KI → Preise).
Feldnamen nach SerpApi-Doku bzw. Projekten, die sie nutzen; aus der Cloud nicht live prüfbar.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from . import config, store
from .models import Accommodation, TransportSegment, new_id

URL = "https://serpapi.com/search.json"
UA = "Kompass/1 (privater Reiseplaner)"
CACHE_H = 6


class PriceError(Exception):
    pass


def available() -> bool:
    return config.FAKE_MODE or bool(store.key("serpapi"))


def month_usage() -> int:
    since = date.today().replace(day=1).isoformat()
    (n,) = store.db().execute("SELECT COUNT(*) FROM costs WHERE purpose LIKE 'preise_%' AND model = 'serpapi' "
                              "AND ts >= ?", (since,)).fetchone()
    return int(n)


def _search(params: dict, purpose: str, trip_id: str | None) -> dict:
    key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:24]
    hit = store.get("prices_cache", key)
    if hit and datetime.fromisoformat(hit["ts"]) > datetime.now() - timedelta(hours=CACHE_H):
        return hit["data"]
    api_key = store.key("serpapi")
    if not api_key:
        raise PriceError("Kein SerpApi-Schlüssel hinterlegt (Profil → KI → Preise).")
    q = urllib.parse.urlencode({**params, "api_key": api_key})
    req = urllib.request.Request(f"{URL}?{q}", headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        if e.code == 401:
            raise PriceError("SerpApi-Schlüssel abgelehnt – bitte prüfen.")
        if e.code == 429:
            raise PriceError("SerpApi: Monatskontingent aufgebraucht oder zu viele Anfragen.")
        raise PriceError(f"SerpApi-Fehler {e.code}: {_msg(body)}")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise PriceError(f"SerpApi nicht erreichbar ({type(e).__name__}).")
    if data.get("error"):
        raise PriceError(f"SerpApi: {data['error']}")
    store.add_cost(purpose, "serpapi", 0.0, 0, 0, trip_id)  # zählt das Kontingent
    store.put("prices_cache", key, {"ts": datetime.now().isoformat(), "data": data})
    return data


def _msg(body: str) -> str:
    try:
        return str(json.loads(body).get("error"))[:200]
    except (ValueError, AttributeError):
        return body[:200]


def _dt(s: str | None) -> datetime | None:
    try:
        return datetime.strptime((s or "").strip()[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None


# --- Flüge ----------------------------------------------------------------------------------

def parse_flights(data: dict, limit: int = 6) -> list[dict]:
    out = []
    for item in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        segs = item.get("flights") or []
        if not segs or item.get("price") in (None, ""):
            continue
        dep = _dt((segs[0].get("departure_airport") or {}).get("time"))
        arr = _dt((segs[-1].get("arrival_airport") or {}).get("time"))
        if not dep or not arr or arr <= dep:
            continue
        via = [lay.get("name") or lay.get("id") for lay in item.get("layovers") or [] if lay.get("name") or lay.get("id")]
        airlines = list(dict.fromkeys(s.get("airline") for s in segs if s.get("airline")))
        numbers = [s.get("flight_number") for s in segs if s.get("flight_number")]
        out.append({"depart": dep.isoformat(), "arrive": arr.isoformat(), "price": float(item["price"]),
                    "carrier": " / ".join(airlines)[:80], "number": ", ".join(numbers)[:60], "via": via,
                    "duration_min": int(item.get("total_duration") or (arr - dep).total_seconds() // 60),
                    "from_code": (segs[0].get("departure_airport") or {}).get("id"),
                    "to_code": (segs[-1].get("arrival_airport") or {}).get("id")})
        if len(out) >= limit:
            break
    return out


def flights(seg: TransportSegment, max_stops: int, trip_id: str) -> dict:
    if seg.mode != "flug":
        raise PriceError("Echte Preise gibt es nur für Flüge.")
    if not seg.origin_code or not seg.destination_code:
        raise PriceError("Für diesen Flug fehlen die Flughafen-Codes (z. B. ZRH → LIS).")
    if seg.depart.date() < date.today():
        raise PriceError("Der Flug liegt in der Vergangenheit.")
    params = {"engine": "google_flights", "departure_id": seg.origin_code, "arrival_id": seg.destination_code,
              "outbound_date": seg.depart.date().isoformat(), "type": 2, "adults": 2, "currency": "CHF",
              "hl": "de", "gl": "ch", "stops": {0: 1, 1: 2, 2: 3}.get(max_stops, 0)}
    data = _fake_flights(seg) if config.FAKE_MODE else _search(params, "preise_flug", trip_id)
    if config.FAKE_MODE:
        store.add_cost("preise_flug", "serpapi", 0.0, 0, 0, trip_id)
    return {"options": parse_flights(data), "url": (data.get("search_metadata") or {}).get("google_flights_url"),
            "note": "Preis für 2 Erwachsene laut Google Flights, Stand jetzt – vor dem Buchen prüfen."}


# --- Hotels ---------------------------------------------------------------------------------

def _num(v) -> float | None:
    if isinstance(v, dict):
        v = v.get("extracted_lowest") or v.get("extracted_before_taxes_fees")
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_hotels(data: dict, nights: int, max_night: float | None, limit: int = 8) -> list[dict]:
    out = []
    for p in data.get("properties") or []:
        night = _num(p.get("rate_per_night"))
        total = _num(p.get("total_rate")) or (night * nights if night else None)
        if not p.get("name") or night is None:
            continue
        out.append({"name": str(p["name"])[:160], "night": night, "total": total,
                    "rating": p.get("overall_rating"), "reviews": p.get("reviews"),
                    "stars": p.get("extracted_hotel_class") or p.get("hotel_class"),
                    "link": p.get("link") or p.get("serpapi_property_details_link"),
                    "area": p.get("neighborhood") or "",
                    "image": next((i.get("thumbnail") or i.get("original_image") for i in p.get("images") or []
                                   if isinstance(i, dict) and (i.get("thumbnail") or i.get("original_image"))), None),
                    "over_budget": bool(max_night and night > max_night)})
    out.sort(key=lambda h: (h["over_budget"], -(h["rating"] or 0), h["night"]))
    return out[:limit]


def hotels(stay: Accommodation, max_night: float | None, trip_id: str) -> dict:
    if stay.check_in < date.today():
        raise PriceError("Die Unterkunft liegt in der Vergangenheit.")
    nights = (stay.check_out - stay.check_in).days
    q = f"Hotels {stay.place.name}" if "(gesucht)" in stay.name or "noch offen" in stay.name else \
        f"{stay.name} {stay.place.name}"
    params = {"engine": "google_hotels", "q": q, "check_in_date": stay.check_in.isoformat(),
              "check_out_date": stay.check_out.isoformat(), "adults": 2, "currency": "CHF", "gl": "ch", "hl": "de"}
    data = _fake_hotels(stay) if config.FAKE_MODE else _search(params, "preise_hotel", trip_id)
    if config.FAKE_MODE:
        store.add_cost("preise_hotel", "serpapi", 0.0, 0, 0, trip_id)
    opts = parse_hotels(data, nights, max_night)
    if not opts and not q.startswith("Hotels "):  # bestimmtes Hotel nicht gefunden: allgemein am Ort suchen
        params["q"] = f"Hotels {stay.place.name}"
        data = _fake_hotels(stay) if config.FAKE_MODE else _search(params, "preise_hotel", trip_id)
        opts = parse_hotels(data, nights, max_night)
    from . import photos  # Bilder der Angebote selbst holen (nur erlaubte Bild-Server)
    for o in opts:
        o["photo"] = photos.download(o.pop("image"), "Google Hotels", o["name"]) if o.get("image") else None
    return {"options": opts, "nights": nights,
            "note": "Preise für 2 Erwachsene laut Google Hotels, Stand jetzt – Verfügbarkeit beim Buchen prüfen."}


# --- Auswahl merken (Übernehmen nur mit Kennung, nie mit Daten aus dem Browser) ----------------

def remember(kind: str, trip_id: str, item_id: str, result: dict) -> str:
    token = new_id()
    store.put("price_pick", token, {"kind": kind, "trip_id": trip_id, "item_id": item_id,
                                    "options": result["options"], "url": result.get("url")})
    return token


def picked(token: str, index: int) -> tuple[dict, dict]:
    data = store.get("price_pick", token)
    if not data or not 0 <= index < len(data["options"]):
        raise KeyError(token)
    return data, data["options"][index]


# --- Testmodus ------------------------------------------------------------------------------

def _fake_flights(seg: TransportSegment) -> dict:
    d = seg.depart.date().isoformat()
    return {"search_metadata": {"google_flights_url": "https://www.google.com/travel/flights?test=1"},
            "best_flights": [{"flights": [{"departure_airport": {"id": seg.origin_code, "time": f"{d} 07:10"},
                                           "arrival_airport": {"id": seg.destination_code, "time": f"{d} 09:35"},
                                           "airline": "Testair", "flight_number": "TA 100"}],
                              "layovers": [], "total_duration": 145, "price": 389}],
            "other_flights": [{"flights": [{"departure_airport": {"id": seg.origin_code, "time": f"{d} 12:00"},
                                            "arrival_airport": {"id": "MAD", "time": f"{d} 14:05"},
                                            "airline": "Iberia", "flight_number": "IB 3471"},
                                           {"departure_airport": {"id": "MAD", "time": f"{d} 15:20"},
                                            "arrival_airport": {"id": seg.destination_code, "time": f"{d} 15:40"},
                                            "airline": "Iberia", "flight_number": "IB 3102"}],
                               "layovers": [{"name": "Madrid", "id": "MAD", "duration": 75}],
                               "total_duration": 280, "price": 268}]}


def _fake_hotels(stay: Accommodation) -> dict:
    return {"properties": [
        {"name": f"Hotel {stay.place.name} Mar", "rate_per_night": {"lowest": "CHF 145", "extracted_lowest": 145},
         "total_rate": {"extracted_lowest": 145 * (stay.check_out - stay.check_in).days}, "overall_rating": 4.6,
         "reviews": 812, "extracted_hotel_class": 4, "link": "https://example.com/mar",
         "images": [{"thumbnail": "https://lh3.googleusercontent.com/fake-hotel-mar"}]},
        {"name": f"Pousada {stay.place.name}", "rate_per_night": {"extracted_lowest": 98}, "overall_rating": 4.4,
         "reviews": 210, "link": "https://example.com/pousada"},
        {"name": f"Grand {stay.place.name}", "rate_per_night": {"extracted_lowest": 420}, "overall_rating": 4.8,
         "reviews": 1500, "extracted_hotel_class": 5}]}
