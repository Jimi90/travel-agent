"""Währungskurse für den Währungsrechner (Helfer).

Quelle: open.er-api.com (ExchangeRate-API, offene Schnittstelle, gratis, ohne Schlüssel, einmal täglich neu,
über 150 Währungen – auch VND, THB, AED, QAR). Die Nutzungsbedingungen verlangen einen Quellenhinweis; er steht
in der Oberfläche unter dem Rechner.

Der Server holt die Kurse (der Browser darf wegen der CSP nur zu sich selbst), merkt sie 6 Stunden im Speicher und
dauerhaft in der Datenbank. Ist der Dienst nicht erreichbar, gelten die zuletzt gespeicherten Kurse mit Hinweis
(«stale»). Aus der Cloud-Testumgebung nicht erreichbar – dort nur mit nachgebildeten Antworten geprüft.
"""
from __future__ import annotations

import json
import re
import threading
import time as _time
import urllib.request
from datetime import datetime, timezone

from . import config, store

URL = "https://open.er-api.com/v6/latest/CHF"
UA = "Kompass/1.0 (privater Reiseplaner)"
TTL = 6 * 3600
BASE = "CHF"
SOURCE = "ExchangeRate-API (open.er-api.com)"
ATTRIBUTION_URL = "https://www.exchangerate-api.com"

_lock = threading.Lock()
_mem: dict = {"until": 0.0, "data": None}

# Testkurse (1 CHF = …), nur im Testmodus
FAKE_RATES = {"CHF": 1.0, "EUR": 1.07, "USD": 1.16, "GBP": 0.92, "THB": 40.2, "VND": 29500.0, "AED": 4.26,
              "QAR": 4.22, "JPY": 175.0, "TRY": 40.0}


class FxError(Exception):
    pass


def _valid(rates) -> dict[str, float]:
    """Nur Dreibuchstaben-Codes mit sinnvollem positivem Kurs."""
    out: dict[str, float] = {}
    if not isinstance(rates, dict):
        return out
    for code, v in rates.items():
        if isinstance(code, str) and re.fullmatch(r"[A-Z]{3}", code) and isinstance(v, (int, float)) \
                and not isinstance(v, bool) and 0 < v < 1e12:
            out[code] = float(v)
    return out


def _fetch() -> dict:
    req = urllib.request.Request(URL, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = json.loads(r.read(2_000_000))
    except Exception as e:  # Netz, HTTP, JSON
        raise FxError(f"Kursdienst nicht erreichbar ({type(e).__name__})") from e
    if not isinstance(raw, dict) or raw.get("result") != "success":
        raise FxError("Kursdienst hat keine Kurse geliefert")
    rates = _valid(raw.get("rates"))
    if rates.get(BASE) != 1.0 or len(rates) < 20:
        raise FxError("Kursdienst: unerwartete Antwort")
    ts = raw.get("time_last_update_unix")
    when = (datetime.fromtimestamp(ts, timezone.utc) if isinstance(ts, (int, float)) else datetime.now(timezone.utc))
    return {"base": BASE, "rates": rates, "updated": when.isoformat(), "fetched": _time.time()}


def get() -> dict:
    """Aktuelle Kurse: {base, rates, updated, source, attribution_url, stale}. 1 CHF = rates[code]."""
    if config.FAKE_MODE:
        return {"base": BASE, "rates": FAKE_RATES, "updated": "2026-11-04T00:00:00+00:00", "source": "Testkurse",
                "attribution_url": ATTRIBUTION_URL, "stale": False}
    with _lock:
        if _mem["data"] and _mem["until"] > _time.time():
            return {**_mem["data"], "stale": False}
    try:
        data = _fetch()
    except FxError as e:
        saved = store.get("fx", "latest")
        if saved and _valid(saved.get("rates")):
            return {**saved, "source": SOURCE, "attribution_url": ATTRIBUTION_URL, "stale": True}
        raise e
    data = {**data, "source": SOURCE, "attribution_url": ATTRIBUTION_URL}
    store.put("fx", "latest", {k: data[k] for k in ("base", "rates", "updated", "fetched")})
    with _lock:
        _mem["data"], _mem["until"] = data, _time.time() + TTL
    return {**data, "stale": False}


def reset_for_tests() -> None:
    with _lock:
        _mem["data"], _mem["until"] = None, 0.0
