"""Restaurants im Tagesplan: Link zu Google Maps und Google-Bewertung (Wunsch James).

Der Link (Google-Maps-Suche nach «Name, Ort») ist gratis und immer da. Die Bewertung (Sterne + Anzahl) holt Kompass
über SerpApi (Google Maps) im Hintergrund, wie bei Hotels und Flügen – eine Suche je Lokal, dauerhaft gemerkt (kv
«venue_live», auch «nicht gefunden»), höchstens ``MAX_SEARCHES_PER_RUN`` pro Lauf und ``MONTH_CAP`` pro Monat, damit
das Gratis-Kontingent (250) für Preise und Fotos reicht. Feldnamen nach SerpApi-Doku (aus der Cloud nicht prüfbar).
"""
from __future__ import annotations

import re
import threading
import urllib.parse
from datetime import date, datetime, timedelta

from . import config, prices, store
from .hotels import same_hotel

MAX_SEARCHES_PER_RUN = 10
MONTH_CAP = 60
RETRY_ERROR_H = 6
PURPOSE = "preise_restaurant"

# «Mittagessen im Riverside Restaurant» → «Riverside Restaurant» (für ältere Pläne ohne «venue»)
_MEAL = re.compile(r"^\s*(?:gemeinsames\s+)?(?:frühstück|brunch|mittagessen|lunch|abendessen|dinner|znacht|zmittag|"
                   r"apéro|apero|aperitif|drinks?|kaffee|cocktails?|essen|nachtessen|imbiss|snack|degustation|"
                   r"weindegustation|street[- ]?food)\b\s*(?:im|in der|in|bei|beim|auf der|auf dem|am|an der|"
                   r"im restaurant|in der bar)?\s*", re.I)
_GENERIC = {"hotel", "unterkunft", "resort", "zimmer", "restaurant", "café", "cafe", "bar", "markt", "nachtmarkt",
            "altstadt", "strand", "hafen", "lokal", "lokalen", "einheimischen", "guesthouse", "riad"}

_lock = threading.Lock()
_running: set[str] = set()


def venue_of(a) -> str | None:
    """Name des Lokals: von der KI (`venue`) oder aus dem Titel. Nur bei Essen & Trinken, nie fürs Hotel-Frühstück."""
    if getattr(a, "category", None) != "kulinarik":
        return None
    v = (getattr(a, "venue", None) or "").strip()
    if not v:
        rest = _MEAL.sub("", a.title, count=1).strip(" -–:,") if _MEAL.match(a.title) else ""
        v = re.sub(r"\s*\(.*?\)\s*$", "", rest).strip()
    words = [w for w in re.findall(r"[\wÀ-ÿ'’]+", v) if w.lower() not in _GENERIC]
    if not any(w[0].isupper() and len(w) >= 3 for w in words):
        return None  # «im Hotel», «am Nachtmarkt», «lokale Küche» – kein bestimmtes Lokal
    if re.fullmatch(r"(?i)\s*(?:im\s+)?(?:hotel|unterkunft)\b.*", v) and len(words) <= 1:
        return None
    return v[:120]


def maps_url(name: str, place: str, place_id: str | None = None) -> str:
    q = {"api": "1", "query": f"{name}, {place}".strip(", ")}
    if place_id:
        q["query_place_id"] = place_id
    return "https://www.google.com/maps/search/?" + urllib.parse.urlencode(q)


def key(name: str, place: str) -> str:
    return f"{name.strip().lower()}|{place.strip().lower()}"


def cached(name: str, place: str) -> dict | None:
    return store.get("venue_live", key(name, place))


def month_usage() -> int:
    since = date.today().replace(day=1).isoformat()
    (n,) = store.db().execute("SELECT COUNT(*) FROM costs WHERE purpose = ? AND model = 'serpapi' AND ts >= ?",
                              (PURPOSE, since)).fetchone()
    return int(n)


def _due(name: str, place: str) -> bool:
    c = cached(name, place)
    if c is None:
        return True
    return bool(c.get("error")) and datetime.now() - datetime.fromisoformat(c["ts"]) > timedelta(hours=RETRY_ERROR_H)


def _items(it):
    for d in it.days:
        for a in d.activities:
            v = venue_of(a)
            if v:
                yield a, v, (a.place.name if a.place else d.base.name)


def missing(it) -> bool:
    return prices.available() and month_usage() < MONTH_CAP and any(_due(v, p) for _, v, p in _items(it))


def pick(data: dict, name: str, place: str) -> dict | None:
    """Treffer aus der Google-Maps-Antwort: eindeutiger Ort (place_results) oder passender Eintrag der Liste."""
    cands = [data["place_results"]] if isinstance(data.get("place_results"), dict) else []
    cands += [r for r in (data.get("local_results") or []) if isinstance(r, dict)]
    for r in cands:
        if r.get("title") and same_hotel(name, str(r["title"]), place):
            return r
    return None


def fetch(name: str, place: str, trip_id: str | None) -> dict:
    result: dict = {"ts": datetime.now().isoformat(), "found": False}
    try:
        if config.FAKE_MODE:
            data = _fake(name)
        else:
            data = prices._search({"engine": "google_maps", "type": "search", "q": f"{name} {place}", "hl": "de"},
                                  PURPOSE, trip_id)
        r = pick(data, name, place)
        if r:
            rating = r.get("rating")
            result.update(found=True, title=str(r.get("title"))[:120],
                          rating=float(rating) if isinstance(rating, (int, float)) else None,
                          reviews=int(r["reviews"]) if isinstance(r.get("reviews"), int) else None,
                          address=str(r.get("address") or "")[:160], place_id=str(r.get("place_id") or "")[:200] or None)
    except prices.PriceError as e:
        result["error"] = str(e)
    store.put("venue_live", key(name, place), result)
    return result


def enrich(it) -> None:
    """Im Hintergrund fehlende Bewertungen holen (SerpApi)."""
    with _lock:
        if it.id in _running:
            return
        _running.add(it.id)

    def run():
        n = 0
        try:
            seen = set()
            for _, v, p in _items(it):
                if n >= MAX_SEARCHES_PER_RUN or month_usage() >= MONTH_CAP:
                    return
                k = key(v, p)
                if k in seen or not _due(v, p):
                    continue
                seen.add(k)
                fetch(v, p, it.id)
                n += 1
        except Exception as e:  # nie die App stören
            print(f"[Restaurants] {it.id}: {type(e).__name__}: {e}", flush=True)
        finally:
            with _lock:
                _running.discard(it.id)

    if config.FAKE_MODE and getattr(enrich, "sync", False):
        run()
    else:
        threading.Thread(target=run, daemon=True, name=f"lokale-{it.id}").start()


def for_trip(it) -> dict:
    """Pro Programmpunkt: {name, url, rating, reviews} – Link immer, Bewertung wenn gefunden."""
    out = {}
    for a, v, p in _items(it):
        c = cached(v, p) or {}
        found = c.get("found")
        out[a.id] = {"name": v, "url": maps_url(c.get("title") or v, p, c.get("place_id") if found else None),
                     "rating": c.get("rating") if found else None, "reviews": c.get("reviews") if found else None}
    return out


def _fake(name: str) -> dict:
    h = sum(map(ord, name))
    return {"local_results": [{"title": name, "rating": round(4.0 + (h % 9) / 10, 1), "reviews": 120 + h % 2000,
                               "address": "Testgasse 1", "place_id": f"test{h}"}]}
