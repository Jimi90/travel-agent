"""Steuerzentrale: verbindet Planer, Umplanung, Ausfall-Behandlung, Wetter, Meldungen und Zeitplan.

Alle Änderungen an Reiseplänen laufen über den einen StatusManager (atomar, mit Protokoll).
"""
from __future__ import annotations

import threading
import time as _time
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config, llm, photos, store, weather
from .core.status_manager import StatusManager
from .models import (
    ChangeSet, ChangeStatus, ClosureDisruption, Itinerary, Place, SetTripStatus, TransportDisruption,
    WeatherDisruption, new_id,
)
from .planning import destinations, generator, variants
from .planning.alternatives import AlternativeActivitiesModule
from .planning.disruption_ext import ExtendedDisruptionHandler
from .planning.providers import PlanAlternatives
from .planning.replanning import AutonomousRePlanningModule

_sm: StatusManager | None = None
_sm_lock = threading.Lock()


def sm() -> StatusManager:
    global _sm
    with _sm_lock:
        if _sm is None:
            config.ensure_dirs()
            _sm = StatusManager(config.DB_PATH)
        return _sm


def reset_for_tests() -> None:
    global _sm
    with _sm_lock:
        if _sm is not None:
            _sm.close()
        _sm = None
    store.reset_connection()
    _jobs.clear()


# --- Ortszeit -------------------------------------------------------------------------------

def tz_of(place: Place) -> ZoneInfo:
    try:
        return ZoneInfo(place.tz) if place.tz else ZoneInfo("Europe/Zurich")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Europe/Zurich")


def local_now(itin: Itinerary) -> datetime:
    """Jetzt am aktuellen Aufenthaltsort (ohne Zeitzone, wie alle Zeiten im Plan)."""
    zurich_today = datetime.now(ZoneInfo("Europe/Zurich")).date()
    day = itin.day(zurich_today) or (itin.days[0] if itin.days else None)
    tz = tz_of(day.base) if day else ZoneInfo("Europe/Zurich")
    return datetime.now(tz).replace(tzinfo=None)


def current_day(itin: Itinerary):
    now = local_now(itin)
    return now, itin.day(now.date())


# --- Bausteine ------------------------------------------------------------------------------

def _modules(itin: Itinerary, clock=None):
    h = store.household()
    alts = AlternativeActivitiesModule(PlanAlternatives(itin, h), h)
    clock = clock or (lambda: local_now(itin))
    return h, alts, clock


def weather_check(trip_id: str, auto: bool = False) -> dict:
    """Wetter am heutigen Ort prüfen und den Tagesplan bei Bedarf umstellen."""
    itin = sm().get(trip_id)
    now, day = current_day(itin)
    if day is None:
        return {"status": "nichts_zu_tun", "message": "Heute ist kein Reisetag."}
    try:
        fc_all = weather.forecast(day.base)
    except weather.WeatherError as e:
        if auto:
            store.notify("fehler", "Wettercheck fehlgeschlagen", str(e), trip_id)
        return {"status": "fehler", "message": str(e)}
    fc = fc_all.get(day.date)
    if fc is None:
        return {"status": "fehler", "message": "Keine Prognose für heute."}
    h, alts, clock = _modules(itin)
    ev = WeatherDisruption(
        id=f"wetter:{trip_id}:{day.date.isoformat()}" if auto else new_id(), itinerary_id=trip_id,
        detected_at=now, place=day.base, date_from=day.date, date_to=day.date, description=fc.summary,
        source="autonomer_wettercheck" if auto else "nutzer", forecasts=[fc])
    mod = AutonomousRePlanningModule(sm(), alts, h, forecast_lookup=lambda p, d: fc_all.get(d) if p.same_area(day.base)
                                     else weather.lookup(p, d), clock=clock)
    out = mod.handle(ev)
    weather_txt = _weather_text(fc)
    msg = out.message()
    if out.status is ChangeStatus.APPLIED:
        store.notify("umplanung", f"Plan angepasst: {day.base.name}, {weather_txt}", msg, trip_id)
    elif out.needs_user:
        store.notify("hinweis", f"Wetter {day.base.name}: {weather_txt}", msg, trip_id)
    elif auto and out.status is not ChangeStatus.DUPLICATE:
        store.notify("info", f"Wettercheck {day.base.name}: {weather_txt}", "Alles passt – nichts geändert.", trip_id)
    return {"status": out.status.value, "message": msg, "weather": weather_txt,
            "decisions": [d.model_dump(mode="json") for d in out.decisions], "needs_user": out.needs_user}


def _weather_text(fc) -> str:
    t = f"{fc.temp_min_c:.0f}–{fc.temp_max_c:.0f} °C" if fc.temp_max_c is not None and fc.temp_min_c is not None else ""
    parts = [fc.summary or "", t, f"{fc.precip_mm:.0f} mm" if fc.precip_mm else ""]
    return ", ".join(p for p in parts if p)


def report_transport(trip_id: str, segment_id: str, days: int, reason: str) -> dict:
    itin = sm().get(trip_id)
    h, alts, clock = _modules(itin)
    seg = next((t for t in itin.transports if t.id == segment_id), None)
    if seg is None:
        return {"status": "abgelehnt", "message": "Verbindung nicht gefunden."}
    ev = TransportDisruption(id=new_id(), itinerary_id=trip_id, detected_at=clock(), place=seg.origin,
                             date_from=seg.depart.date(), date_to=seg.depart.date() + timedelta(days=days - 1),
                             source="nutzer", segment_id=segment_id, delay_days=days, description=reason)
    out = ExtendedDisruptionHandler(sm(), alts, h, clock).handle(ev)
    if out.status is ChangeStatus.APPLIED:
        store.notify("ausfall", f"Ausfall verarbeitet: {seg.origin.name} → {seg.destination.name}", out.message(), trip_id)
    return {"status": out.status.value, "message": out.message(), "summary": out.summary, "warnings": out.warnings}


def report_closure(trip_id: str, activity_id: str, reason: str) -> dict:
    itin = sm().get(trip_id)
    found = itin.find_activity(activity_id)
    if not found:
        return {"status": "abgelehnt", "message": "Programmpunkt nicht gefunden."}
    day, a = found
    h, alts, clock = _modules(itin)
    ev = ClosureDisruption(id=new_id(), itinerary_id=trip_id, detected_at=clock(), place=day.base,
                           date_from=day.date, date_to=day.date, source="nutzer", venue=a.title,
                           activity_id=activity_id, description=reason or "fällt aus")
    out = AutonomousRePlanningModule(sm(), alts, h, clock=clock).handle_closure(ev)
    return {"status": out.status.value, "message": out.message(), "needs_user": out.needs_user}


def change(trip_id: str, ops: list, reason: str, acknowledged: list[str] | None = None) -> dict:
    """Änderung von Hand (Status, festnageln, bestätigen …) – ebenfalls über den StatusManager."""
    itin = sm().get(trip_id)
    res = sm().apply(ChangeSet(itinerary_id=trip_id, base_version=itin.version, operations=ops, reason=reason,
                               actor="nutzer", acknowledged_locked=acknowledged or []),
                     store.household().constraints.model_copy(update={"budget_total_max": None}), local_now(itin))
    return {"status": res.status.value, "problems": [p.message for p in res.problems]}


# --- Hintergrund-Aufträge (Planen dauert bis zu einigen Minuten) ---------------------------------

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def job(job_id: str) -> dict | None:
    with _jobs_lock:
        j = _jobs.get(job_id)
        return dict(j) if j else None


def _start_job(kind: str, fn, *args) -> str:
    jid = new_id()
    with _jobs_lock:
        running = [j for j in _jobs.values() if j["status"] == "laeuft"]
        if len(running) >= 2:
            raise llm.LLMError("Es laufen schon zwei Aufträge – bitte kurz warten.")
        _jobs[jid] = {"id": jid, "kind": kind, "status": "laeuft", "started": datetime.now().isoformat()}

    def run():
        try:
            result = fn(*args)
            upd = {"status": "fertig", "result": result}
        except (llm.LLMError, weather.WeatherError) as e:
            upd = {"status": "fehler", "error": str(e)}
        except Exception as e:  # unerwartet: ins Protokoll, verständliche Meldung
            traceback.print_exc()
            upd = {"status": "fehler", "error": f"Unerwarteter Fehler ({type(e).__name__})."}
        with _jobs_lock:
            _jobs[jid].update(upd, finished=datetime.now().isoformat())

    threading.Thread(target=run, daemon=True, name=f"job-{kind}").start()
    return jid


def start_destinations(start: date, days: int, wish: str) -> str:
    s = store.settings()

    def run():
        res = destinations.suggest(store.household(), start, days, wish, s["home_airport"])
        res["id"] = new_id()
        store.put("ideas", res["id"], res)
        return res

    return _start_job("ziele", run)


def start_variants(destination: str, start: date, days: int, wishes: str) -> str:
    s = store.settings()

    def run():
        res = variants.suggest(store.household(), destination, start, days, wishes, s["home_airport"])
        store.put("variants", res["id"], res)
        return res

    return _start_job("varianten", run)


def start_plan(destination: str, start: date, days: int, wishes: str, route: list[dict] | None = None,
               summary: str = "") -> str:
    s = store.settings()

    def run():
        itin, notes = generator.generate(store.household(), destination, start, days, wishes, s["home_airport"],
                                         web_search=bool(s.get("web_search")), route=route)
        if summary:  # gewählte Variante zuerst, dann der Überblick des Planers
            itin = itin.model_copy(update={"summary": f"{summary} {itin.summary}".strip()})
        sm().create(itin)
        store.put("plan_notes", itin.id, notes)
        photos.fill_trip(itin)
        # Kosten des Planens der neuen Reise zuordnen
        store.db().execute("UPDATE costs SET trip_id = ? WHERE trip_id IS NULL AND purpose = 'reiseplan' "
                           "AND ts >= ?", (itin.id, (datetime.now() - timedelta(minutes=30)).isoformat()))
        store.notify("plan", f"Neue Reise: {itin.title}", f"{len(itin.days)} Tage, ca. {itin.total_cost():.0f} CHF "
                     "(Schätzung).", itin.id)
        return {"trip_id": itin.id, "notes": notes}

    return _start_job("reiseplan", run)


# --- Zeitplan: jeden Morgen Wetter prüfen -------------------------------------------------------

def scheduler_tick() -> list[str]:
    """Einmal pro Minute: für aktive Reisen nach der eingestellten Ortszeit den Wettercheck starten."""
    s = store.settings()
    done = []
    try:
        hh, mm = (int(x) for x in str(s.get("check_time", "06:30")).split(":")[:2])
    except ValueError:
        hh, mm = 6, 30
    for itin in sm().list():
        if itin.status not in ("gebucht", "unterwegs"):
            continue
        now, day = current_day(itin)
        if now.date() > itin.end_date and itin.status == "unterwegs":
            change(itin.id, [SetTripStatus(status="abgeschlossen")], "Reise beendet")
            continue
        if day is None:
            continue
        if itin.status == "gebucht":
            change(itin.id, [SetTripStatus(status="unterwegs")], "Reise hat begonnen")
        if not s.get("auto_check", True) or (now.hour, now.minute) < (hh, mm):
            continue
        key = f"{itin.id}:{now.date().isoformat()}"
        if store.get("checked", key):
            continue
        store.put("checked", key, {"ts": datetime.now().isoformat()})
        try:
            weather_check(itin.id, auto=True)
            done.append(itin.id)
        except Exception:
            traceback.print_exc()
    return done


def start_scheduler() -> None:
    def loop():
        while True:
            try:
                scheduler_tick()
            except Exception:
                traceback.print_exc()
            _time.sleep(60)

    threading.Thread(target=loop, daemon=True, name="zeitplan").start()
