"""Echte Flugdaten automatisch (SerpApi Google Flights) – im Hintergrund geholt und dauerhaft gemerkt.

Wie bei den Hotels (``hotels.py``): nach dem Planen bzw. wenn etwas fehlt, holt Kompass für jeden geschätzten Flug
mit Flughafen-Codes die aktuellen Angebote. Die Seite zeigt dann konkrete Zeiten, Airline und Preis; «Übernehmen»
trägt das gewählte Angebot in den Plan ein (über ``/prices/apply``, nie ungefragt). Eine Suche kostet 1 von 250
Gratis-Suchen pro Monat, deshalb: nur Flüge in der Zukunft, nur alle ``REFRESH_DAYS`` Tage neu, Fehler 6 Std. merken.
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta

from . import config, prices, store

REFRESH_DAYS = 2
RETRY_ERROR_H = 6
MAX_SEARCHES_PER_RUN = 6
SHOWN = 3  # so viele Angebote zeigt die Seite

_lock = threading.Lock()
_running: set[str] = set()


def key(seg) -> str:
    return f"{seg.origin_code}-{seg.destination_code}-{seg.depart.date().isoformat()}"


def cached(seg) -> dict | None:
    return store.get("flight_live", key(seg))


def eligible(seg) -> bool:
    return (seg.mode == "flug" and seg.estimated and bool(seg.origin_code and seg.destination_code)
            and date.today() <= seg.depart.date() <= date.today() + timedelta(days=330) and prices.available())


def _due(seg) -> bool:
    c = cached(seg)
    if c is None:
        return True
    age = datetime.now() - datetime.fromisoformat(c["ts"])
    return age > (timedelta(hours=RETRY_ERROR_H) if c.get("error") else timedelta(days=REFRESH_DAYS))


def missing(it) -> bool:
    return it.status in ("entwurf", "gebucht") and any(eligible(t) and _due(t) for t in it.transports)


def fetch(seg, trip_id: str) -> dict:
    result: dict = {"ts": datetime.now().isoformat(), "options": []}
    try:
        res = prices.flights(seg, store.household().constraints.max_stops, trip_id)
        result.update(options=res["options"][:SHOWN], url=res.get("url"), note=res.get("note"))
        if res["options"]:
            result["token"] = prices.remember("verbindung", trip_id, seg.id, {**res, "options": res["options"][:SHOWN]})
        else:
            result["error"] = "Google Flights hat für dieses Datum keine Flüge gefunden."
    except prices.PriceError as e:
        result["error"] = str(e)
    store.put("flight_live", key(seg), result)
    return result


def enrich(it) -> None:
    """Im Hintergrund fehlende bzw. veraltete Flugangebote holen."""
    with _lock:
        if it.id in _running:
            return
        _running.add(it.id)

    def run():
        n = 0
        try:
            for seg in it.transports:
                if n >= MAX_SEARCHES_PER_RUN:
                    return
                if eligible(seg) and _due(seg):
                    fetch(seg, it.id)
                    n += 1
        except Exception as e:  # nie die App stören
            print(f"[Flüge] {it.id}: {type(e).__name__}: {e}", flush=True)
        finally:
            with _lock:
                _running.discard(it.id)

    if config.FAKE_MODE and getattr(enrich, "sync", False):
        run()
    else:
        threading.Thread(target=run, daemon=True, name=f"fluege-{it.id}").start()


def for_trip(it) -> dict:
    """Pro Verbindung: {options, url, token, ts, error} – für die Seite."""
    out = {}
    for t in it.transports:
        c = cached(t) if t.mode == "flug" and t.origin_code and t.destination_code else None
        if c:
            out[t.id] = {k: c.get(k) for k in ("options", "url", "token", "ts", "error")}
    return out
