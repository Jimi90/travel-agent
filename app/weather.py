"""Wetter über Open-Meteo (gratis, ohne Schlüssel): Prognose, Klima (Vorjahr) und Ortssuche.

Hinweis: Open-Meteo ist für private Nutzung gratis. Aus der Cloud-Testumgebung nicht erreichbar –
dort nur mit nachgebildeten Antworten geprüft.
"""
from __future__ import annotations

import json
import threading
import time as _time
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta

from . import config
from .models import HourlyWeather, Place, WeatherForecast

UA = "Kompass/1.0 (privater Reiseplaner)"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
THUNDER = {95, 96, 99}
CODES = {0: "klar", 1: "meist klar", 2: "teils bewölkt", 3: "bedeckt", 45: "Nebel", 48: "Nebel",
         51: "Niesel", 53: "Niesel", 55: "Niesel", 61: "leichter Regen", 63: "Regen", 65: "starker Regen",
         71: "Schnee", 73: "Schnee", 75: "starker Schnee", 80: "Schauer", 81: "Schauer", 82: "heftige Schauer",
         95: "Gewitter", 96: "Gewitter mit Hagel", 99: "Gewitter mit Hagel"}

_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


class WeatherError(Exception):
    pass


def _get(url: str, params: dict, ttl: float) -> dict:
    full = url + "?" + urllib.parse.urlencode(params)
    with _lock:
        hit = _cache.get(full)
        if hit and hit[0] > _time.time():
            return hit[1]  # type: ignore[return-value]
    req = urllib.request.Request(full, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as e:  # Netz, HTTP, JSON
        raise WeatherError(f"Wetterdienst nicht erreichbar ({type(e).__name__})") from e
    if isinstance(data, dict) and data.get("error"):
        raise WeatherError(f"Wetterdienst: {data.get('reason', 'Fehler')}")
    with _lock:
        _cache[full] = (_time.time() + ttl, data)
    return data


# --- Ortssuche ------------------------------------------------------------------------------

def geocode(name: str, country: str | None = None) -> Place:
    """Ort per Name suchen; mit Ländercode (ISO-2) nur im richtigen Land (sonst wird «San José de Cúcuta» zu Costa Rica)."""
    if config.FAKE_MODE:
        return _fake_place(name)
    params = {"name": name, "count": 1, "language": "de", "format": "json"}
    if country and len(country) == 2 and country.isalpha():
        params["countryCode"] = country.upper()
    data = _get(GEO_URL, params, 7 * 86400)
    res = (data.get("results") or [])
    if not res:
        raise WeatherError(f"Ort «{name}» nicht gefunden")
    r = res[0]
    return Place(name=r.get("name") or name, country=(r.get("country_code") or "XX")[:2],
                 lat=r["latitude"], lon=r["longitude"], tz=r.get("timezone"))


# --- Prognose -------------------------------------------------------------------------------

def parse_forecast(data: dict, place: Place) -> list[WeatherForecast]:
    """Open-Meteo-Antwort → eine WeatherForecast je Tag (mit stündlichen Werten)."""
    d, h = data.get("daily") or {}, data.get("hourly") or {}
    hourly_by_day: dict[str, list[HourlyWeather]] = {}
    for i, ts in enumerate(h.get("time") or []):
        day, hm = ts.split("T")
        code = _at(h, "weather_code", i)
        hourly_by_day.setdefault(day, []).append(HourlyWeather(
            time=time.fromisoformat(hm), precip_mm=_at(h, "precipitation", i) or 0,
            precip_probability=int(_at(h, "precipitation_probability", i) or 0),
            wind_kmh=_at(h, "wind_speed_10m", i) or 0, thunderstorm=code in THUNDER,
            temp_c=_at(h, "temperature_2m", i)))
    out = []
    for i, day in enumerate(d.get("time") or []):
        code = _at(d, "weather_code", i)
        out.append(WeatherForecast(
            date=date.fromisoformat(day), place=place, summary=CODES.get(code, ""),
            precip_mm=_at(d, "precipitation_sum", i) or 0,
            precip_probability=int(_at(d, "precipitation_probability_max", i) or 0),
            wind_kmh_max=_at(d, "wind_speed_10m_max", i) or 0,
            temp_min_c=_at(d, "temperature_2m_min", i), temp_max_c=_at(d, "temperature_2m_max", i),
            thunderstorm=code in THUNDER, hourly=hourly_by_day.get(day, [])))
    return out


def _at(block: dict, key: str, i: int):
    arr = block.get(key) or []
    return arr[i] if i < len(arr) else None


def forecast(place: Place) -> dict[date, WeatherForecast]:
    """Prognose für die nächsten 16 Tage, nach Datum (Ortszeit)."""
    if config.FAKE_MODE:
        return {f.date: f for f in _fake_forecast(place)}
    if place.lat is None or place.lon is None:
        place = geocode(place.name).model_copy(update={"name": place.name})
    data = _get(FORECAST_URL, {
        "latitude": place.lat, "longitude": place.lon, "timezone": "auto", "forecast_days": 16,
        "hourly": "precipitation,precipitation_probability,wind_speed_10m,weather_code,temperature_2m",
        "daily": "precipitation_sum,precipitation_probability_max,wind_speed_10m_max,"
                 "temperature_2m_max,temperature_2m_min,weather_code",
    }, 3600)
    return {f.date: f for f in parse_forecast(data, place)}


def lookup(place: Place, d: date) -> WeatherForecast | None:
    """Für die Umplanung: Prognose eines Tages oder None (unbekannt/zu weit weg)."""
    try:
        return forecast(place).get(d)
    except WeatherError:
        return None


# --- Klima (gleiche Reisezeit im Vorjahr) ------------------------------------------------------

def climate(lat: float, lon: float, start: date, days: int) -> dict:
    """Durchschnittswerte derselben Tage ein Jahr zuvor: echte Messwerte, keine Prognose."""
    if config.FAKE_MODE:
        return {"temp_max": 24.0, "temp_min": 15.0, "rain_days": 0.2, "year": start.year - 1}
    s = _year_before(start)
    e = s + timedelta(days=max(1, days) - 1)
    data = _get(ARCHIVE_URL, {"latitude": lat, "longitude": lon, "start_date": s.isoformat(),
                              "end_date": e.isoformat(), "timezone": "auto",
                              "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum"}, 30 * 86400)
    d = data.get("daily") or {}
    tmax = [v for v in d.get("temperature_2m_max") or [] if v is not None]
    tmin = [v for v in d.get("temperature_2m_min") or [] if v is not None]
    rain = [v for v in d.get("precipitation_sum") or [] if v is not None]
    if not tmax:
        raise WeatherError("keine Klimadaten")
    return {"temp_max": round(sum(tmax) / len(tmax), 1), "temp_min": round(sum(tmin) / len(tmin), 1),
            "rain_days": round(sum(1 for r in rain if r >= 1) / max(1, len(rain)), 2), "year": s.year}


MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"


def sea_temperature(lat: float, lon: float, start: date, days: int) -> float | None:
    """Meerestemperatur (°C) derselben Tage im Vorjahr (Open-Meteo Marine). None, wenn kein Meer in der Nähe/kein Wert."""
    if config.FAKE_MODE:
        return 21.0
    s = _year_before(start)
    e = s + timedelta(days=max(1, days) - 1)
    try:
        data = _get(MARINE_URL, {"latitude": lat, "longitude": lon, "start_date": s.isoformat(),
                                 "end_date": e.isoformat(), "hourly": "sea_surface_temperature",
                                 "timezone": "auto"}, 30 * 86400)
    except WeatherError:
        return None
    vals = [v for v in (data.get("hourly") or {}).get("sea_surface_temperature") or [] if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _year_before(d: date) -> date:
    try:
        y = d.replace(year=d.year - 1)
    except ValueError:  # 29. Februar
        y = d.replace(year=d.year - 1, day=28)
    # Archiv hinkt einige Tage hinterher: nie näher als 7 Tage an heute
    limit = date.today() - timedelta(days=7)
    while y > limit:
        y = y.replace(year=y.year - 1) if not (y.month == 2 and y.day == 29) else y.replace(year=y.year - 1, day=28)
    return y


# --- Testmodus ------------------------------------------------------------------------------

def _fake_place(name: str) -> Place:
    n = name.strip() or "Ort"
    h = sum(map(ord, n))
    return Place(name=n, country="PT", lat=round(30 + h % 20 + 0.5, 4), lon=round(-9 + h % 30 + 0.5, 4),
                 tz="Europe/Lisbon")


def fake_overrides() -> dict:
    """Testmodus: data/fake_weather.json = {"YYYY-MM-DD": "regen|nachmittag|gewitter|sonne"}."""
    try:
        return json.loads((config.DATA_DIR / "fake_weather.json").read_text())
    except (OSError, ValueError):
        return {}


def _fake_forecast(place: Place) -> list[WeatherForecast]:
    over = fake_overrides()
    today = datetime.now().date()
    out = []
    for i in range(-30, 400):  # grosszügig, damit auch Testreisen in der Zukunft Wetter haben
        d = today + timedelta(days=i)
        kind = over.get(d.isoformat(), "sonne")
        if kind == "regen":
            f = WeatherForecast(date=d, place=place, summary="Regen", precip_mm=18, precip_probability=90,
                                temp_min_c=12, temp_max_c=16)
        elif kind == "nachmittag":
            f = WeatherForecast(date=d, place=place, summary="Regen ab Mittag", precip_mm=10, hourly=[
                HourlyWeather(time=time(hh), precip_mm=0 if hh < 14 else 3, precip_probability=0 if hh < 14 else 95)
                for hh in range(0, 24)])
        elif kind == "gewitter":
            f = WeatherForecast(date=d, place=place, summary="Gewitter", precip_mm=6, thunderstorm=True,
                                wind_kmh_max=60, alerts=["Gewitter mit Sturmböen"])
        else:
            f = WeatherForecast(date=d, place=place, summary="sonnig", temp_min_c=15, temp_max_c=24, wind_kmh_max=10)
        out.append(f)
    return out
