"""Hotelvorschläge je Ort mit echten Preisen und Fotos (Google Hotels über SerpApi).

Pro Hotelvorschlag eine Suche «Hotelname Ort» für genau die Nächte der Unterkunft. Übernommen wird ein Treffer
nur, wenn sein Name wirklich zum Vorschlag passt – nie der Preis eines anderen Hotels. Ergebnisse bleiben
gemerkt (kv «hotel_live»), damit das Gratis-Kontingent (250 Suchen/Monat) geschont wird.
Ohne SerpApi-Schlüssel: wenn möglich ein Foto über Wikipedia, keine Preise.
"""
from __future__ import annotations

import hashlib
import re
import threading
import unicodedata
from datetime import datetime

from . import config, photos, prices, store

MAX_PHOTOS = 4
MAX_SEARCHES_PER_RUN = 15
GENERIC = {"hotel", "hotels", "resort", "the", "and", "spa", "boutique", "suites", "suite", "inn", "hostel", "villa",
           "villas", "residence", "apartments", "lodge", "house", "grand", "city", "centre", "center", "central"}

_lock = threading.Lock()
_running: set[str] = set()


def options(it, stay) -> list:
    """Die 2–3 Hotelvorschläge des Plans für den Ort dieser Unterkunft (exakter Ortsname vor ungefährem)."""
    name = stay.place.name.casefold()
    exact = [h for h in it.hotel_suggestions if h.base.casefold() == name]
    if exact:
        return exact
    return [h for h in it.hotel_suggestions if h.base.casefold() in name or name in h.base.casefold()]


def _words(s: str) -> set[str]:
    f = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().casefold()
    return {w for w in re.findall(r"[a-z0-9]{3,}", f) if w not in GENERIC}


def same_hotel(wanted: str, found: str, place: str = "") -> bool:
    """Passt der Google-Treffer zum Vorschlag? Mindestens ein bezeichnendes Wort gemeinsam (Ortsname zählt nicht)."""
    w, f = _words(wanted) - _words(place), _words(found) - _words(place)
    if not w:  # Name besteht nur aus Ort + Allgemeinem («Hotel Lissabon») → alle Wörter vergleichen
        return _words(wanted) <= _words(found)
    # Ein gemeinsames Wort reicht nur, wenn einer der Namen nur eins hat (sonst: «Saigon» allein ≠ gleiches Hotel)
    need = 2 if len(w) >= 2 and len(f) >= 2 else 1
    return len(w & f) >= need


def key(name: str, place: str, check_in, check_out) -> str:
    raw = f"{name.casefold()}|{place.casefold()}|{check_in}|{check_out}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def cached(name: str, stay) -> dict | None:
    return store.get("hotel_live", key(name, stay.place.name, stay.check_in, stay.check_out))


def _props(data: dict) -> list[dict]:
    if data.get("properties"):
        return data["properties"]
    if data.get("name") and any(k in data for k in ("rate_per_night", "images", "prices", "total_rate")):
        return [data]  # Suche traf direkt ein bestimmtes Hotel
    return []


def fetch(name: str, stay, trip_id: str) -> dict:
    """Eine Suche für diesen Hotelvorschlag; Ergebnis wird gemerkt (auch «nicht gefunden»)."""
    k = key(name, stay.place.name, stay.check_in, stay.check_out)
    nights = (stay.check_out - stay.check_in).days
    result: dict = {"ts": datetime.now().isoformat(), "found": False, "photos": []}
    if prices.available():
        params = {"engine": "google_hotels", "q": f"{name} {stay.place.name}",
                  "check_in_date": stay.check_in.isoformat(), "check_out_date": stay.check_out.isoformat(),
                  "adults": 2, "currency": "CHF", "gl": "ch", "hl": "de"}
        try:
            data = _fake(name) if config.FAKE_MODE else prices._search(params, "preise_hotel", trip_id)
            if config.FAKE_MODE:
                store.add_cost("preise_hotel", "serpapi", 0.0, 0, 0, trip_id)
        except prices.PriceError as e:
            return {**result, "error": str(e)}  # nicht merken: später nochmals versuchen
        prop = next((p for p in _props(data) if same_hotel(name, str(p.get("name", "")), stay.place.name)), None)
        if prop:
            night = prices._num(prop.get("rate_per_night"))
            total = prices.stay_total(prices._num(prop.get("total_rate")), night, nights)
            imgs = [i.get("thumbnail") or i.get("original_image") for i in prop.get("images") or []
                    if isinstance(i, dict)]
            ids = [pid for pid in (photos.download(u, "Google Hotels", name) for u in imgs[:MAX_PHOTOS + 2] if u) if pid]
            result.update(found=True, google_name=str(prop.get("name"))[:160], night=night, total=total,
                          rating=prop.get("overall_rating"), reviews=prop.get("reviews"),
                          link=prop.get("link") or prop.get("serpapi_property_details_link"),
                          photos=ids[:MAX_PHOTOS], source="Google Hotels")
    if not result["photos"]:  # ohne SerpApi oder ohne Bilder: vielleicht hat Wikipedia/Commons eins
        result["photos"] = _free_photos(name, stay.place.name)
    result["v"] = PHOTO_FALLBACK_V
    store.put("hotel_live", k, result)
    return result


PHOTO_FALLBACK_V = 2  # 2: Commons als zweite freie Quelle → ältere Ergebnisse ohne Foto neu versuchen (ohne SerpApi)


def _free_photos(name: str, place: str) -> list:
    pid = photos.wiki_photo(f"{name} {place}", must_match=name) or photos.commons_photo(f"{name} {place}", must_match=name)
    return [pid] if pid else []


def needs_photo_retry(c: dict | None) -> bool:
    return bool(c) and not c.get("photos") and c.get("v") != PHOTO_FALLBACK_V and "error" not in c


def retry_photos(name: str, stay) -> None:
    """Nur die kostenlosen Fotoquellen nochmals fragen; Preise/Suche (SerpApi) bleiben, wie sie sind."""
    k = key(name, stay.place.name, stay.check_in, stay.check_out)
    c = store.get("hotel_live", k)
    if not needs_photo_retry(c):
        return
    c["photos"] = _free_photos(name, stay.place.name)
    c["v"] = PHOTO_FALLBACK_V
    store.put("hotel_live", k, c)


def missing(it) -> bool:
    return any(cached(h.name, st) is None or needs_photo_retry(cached(h.name, st))
               for st in it.stays if not st.confirmed for h in options(it, st))


def enrich(it, force_stay: str | None = None) -> None:
    """Im Hintergrund für alle Hotelvorschläge Preis + Fotos holen (force_stay: diese Unterkunft neu)."""
    with _lock:
        if it.id in _running:
            return
        _running.add(it.id)

    def run():
        n = 0
        try:
            for st in it.stays:
                if st.confirmed and st.id != force_stay:
                    continue
                for h in options(it, st):
                    if n >= MAX_SEARCHES_PER_RUN:
                        return
                    if st.id == force_stay or cached(h.name, st) is None:
                        fetch(h.name, st, it.id)
                        n += 1
                    else:
                        retry_photos(h.name, st)
        except Exception as e:  # nie die App stören
            print(f"[Hotels] {it.id}: {type(e).__name__}: {e}", flush=True)
        finally:
            with _lock:
                _running.discard(it.id)

    if config.FAKE_MODE and getattr(enrich, "sync", False):
        run()
    else:
        threading.Thread(target=run, daemon=True, name=f"hotels-{it.id}").start()


def _fake(name: str) -> dict:
    h = int(hashlib.md5(name.encode()).hexdigest()[:4], 16)
    night = 80 + h % 200
    return {"properties": [
        {"name": f"{name}", "rate_per_night": {"extracted_lowest": night},
         "overall_rating": round(3.8 + (h % 12) / 10, 1), "reviews": 100 + h % 900, "link": "https://example.com/h",
         "images": [{"thumbnail": f"https://lh3.googleusercontent.com/fake-{h}-{i}"} for i in range(5)]},
        {"name": "Ganz anderes Haus", "rate_per_night": {"extracted_lowest": 40}}]}
