"""Webserver von Kompass (Standardbibliothek, Sicherheit wie Hörwerk).

Jede Anfrage läuft durch ``_gate()``: ohne gültige Sitzung nur /login, /health, Manifest und Symbole.
Änderungen (POST/DELETE) nur mit Header ``X-Kompass: 1`` (Schutz vor fremden Seiten).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from pydantic import ValidationError

from . import auth, bookings, config, drafting, llm, photos, prices, service, store, vision
from .models import ChangeSet, Household, SetConfirmed, SetFixed, SetTripStatus, UpdateStay, UpdateTransport
from .models.common import CATEGORY_LABELS
from .planning.matching import match

WEB = Path(__file__).parent / "web"
MAX_BODY = 256 * 1024
MAX_IMAGE_BODY = 8 * 1024 * 1024
STATIC = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/favicon.ico": ("icon.svg", "image/svg+xml"),
}
PUBLIC_GET = {"/health", "/login", *STATIC}
ID = r"[A-Za-z0-9_-]{4,40}"


class BadRequest(Exception):
    pass


def _date(v) -> date:
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        raise BadRequest("Ungültiges Datum.")


def _int(v, lo: int, hi: int, name: str) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise BadRequest(f"{name}: Zahl erwartet.")
    if not lo <= n <= hi:
        raise BadRequest(f"{name}: {lo}–{hi} erlaubt.")
    return n


def _text(v, maxlen: int) -> str:
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(v or "")).strip()
    return s[:maxlen]


# --- Antworten zusammenstellen ----------------------------------------------------------------

def trip_summary(it) -> dict:
    pm = photos.trip_photos(it.id)["places"]
    return {"id": it.id, "title": it.title, "start": it.start_date.isoformat(), "end": it.end_date.isoformat(),
            "photo": next((pm.get(d.base.name) for d in it.days if pm.get(d.base.name)), None),
            "status": it.status, "version": it.version, "total": str(it.total_cost()),
            "place": it.days[0].base.name if it.days else "", "favorite": bool(store.get("fav", it.id))}


def trip_detail(trip_id: str) -> dict:
    sm = service.sm()
    it = sm.get(trip_id)
    now, today = service.current_day(it)
    data = it.model_dump(mode="json")
    for day, raw in zip(it.days, data["days"]):
        for a, ra in zip(day.activities, raw["activities"]):
            ra["locked"] = a.locked(now)
    hist = [h.model_dump(mode="json") for h in sm.history(it.id)]
    for h in hist:
        h["can_undo"] = h["undone_by"] is None and h["to_version"] == it.version and h["operations"] != []
    pending = photos.missing(it)
    if pending:
        photos.fill_trip(it)  # im Hintergrund; die Seite fragt kurz danach nochmals
    tp = photos.trip_photos(it.id)
    hero = next((tp["places"].get(d.base.name) for d in it.days if tp["places"].get(d.base.name)), None)
    hero_meta = (store.get("photo", hero) or {}) if hero else {}
    return {"trip": data, "total": str(it.total_cost()), "now": now.isoformat(),
            "photos": tp, "photos_pending": pending,
            "hero": {"id": hero, "title": hero_meta.get("title", ""), "page": hero_meta.get("page", ""),
                     "source": hero_meta.get("source", "")} if hero else None,
            "today": today.date.isoformat() if today else None, "history": list(reversed(hist)),
            "actions": sm.actions(it.id, status="offen"), "notes": store.get("plan_notes", it.id, []),
            "favorite": bool(store.get("fav", it.id)), "ai_cost": round(store.spent_for_trip(it.id), 4),
            "drafts": {k.split(":", 1)[1]: v for k, v in store.all_of("draft").items() if k.startswith(it.id + ":")}}


def info() -> dict:
    s = store.settings()
    return {"version": config.APP_VERSION, "build": config.APP_BUILD, "fake": config.FAKE_MODE,
            "settings": s, "models": config.MODELS, "keys": store.key_info(), "ai": llm.available(),
            "free_models": {k: v["model"] for k, v in config.FREE_PROVIDERS.items()},
            "prices": {"available": prices.available(), "month": prices.month_usage(), "free_quota": 250},
            "spent_24h": round(store.spent_since(24), 4), "spent_30d": round(store.spent_since(24 * 30), 4),
            "budget_usd": config.DAILY_BUDGET_USD, "unread": store.unread(),
            "categories": {k.value: v for k, v in CATEGORY_LABELS.items()}}


class Handler(BaseHTTPRequestHandler):
    server_version = "Kompass/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/api/jobs/" not in (str(args[0]) if args else ""):
            super().log_message(fmt, *args)

    def end_headers(self):
        for k, v in auth.SECURITY_HEADERS.items():
            self.send_header(k, v)
        super().end_headers()

    def _ip(self):
        # Nur der Tailscale-Container erreicht die App; er hängt die echte Adresse hinten an.
        xff = self.headers.get("X-Forwarded-For", "")
        return xff.split(",")[-1].strip() if xff else self.client_address[0]

    def _cookie(self):
        c = cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return ""
        return c[auth.COOKIE].value if auth.COOKIE in c else ""

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, default=str)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, where):
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _gate(self, method, path):
        """True = Anfrage darf weiter; sonst wurde bereits geantwortet."""
        problem = auth.password_problem()
        if problem and path != "/health":
            html = (WEB / "setup.html").read_text("utf-8").replace("{{GRUND}}", problem)
            self._send(503, html, "text/html; charset=utf-8")
            return False
        if method in ("POST", "DELETE"):
            if self.headers.get(auth.CSRF_HEADER) != "1":
                self._send(403, {"error": "Anfrage nicht erlaubt."})
                return False
            limit = MAX_IMAGE_BODY if path == "/api/vision" or path.endswith("/import") else MAX_BODY
            if int(self.headers.get("Content-Length") or 0) > limit:
                self._send(413, {"error": "Anfrage zu gross."})
                return False
            if path == "/api/login":
                return True
        elif path in PUBLIC_GET:
            return True
        if auth.cookie_ok(self._cookie()):
            return True
        if method == "GET" and not path.startswith("/api/"):
            self._redirect("/login")
        else:
            self._send(401, {"error": "Bitte anmelden."})
        return False

    # ---------- GET ----------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        p = urlparse(self.path).path
        if not self._gate("GET", p):
            return
        try:
            return self._get(p)
        except KeyError:
            return self._send(404, {"error": "nicht gefunden"})

    def _get(self, p):
        if p == "/health":
            return self._send(200, "ok", "text/plain; charset=utf-8")
        if p == "/login":
            return self._send(200, (WEB / "login.html").read_bytes(), "text/html; charset=utf-8")
        if p in STATIC:
            name, ctype = STATIC[p]
            f = WEB / name
            if not f.exists():
                return self._send(404, {"error": "nicht gefunden"})
            return self._send(200, f.read_bytes(), ctype, {"Cache-Control": "public, max-age=86400"})
        if p == "/":
            return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if p == "/api/info":
            return self._send(200, info())
        if m := re.fullmatch(r"/api/photo/([0-9a-f]{24})", p):
            got = photos.get(m.group(1))
            if not got:
                return self._send(404, {"error": "nicht gefunden"})
            data, ctype, _ = got
            return self._send(200, data, ctype, {"Cache-Control": "private, max-age=2592000"})
        if p == "/api/trips":
            return self._send(200, {"trips": [trip_summary(t) for t in service.sm().list()]})
        if m := re.fullmatch(rf"/api/trips/({ID})", p):
            return self._send(200, trip_detail(m.group(1)))
        if p == "/api/household":
            h = store.household()
            return self._send(200, {"household": h.model_dump(mode="json"),
                                    "matching": match(h).model_dump(mode="json"), "prompt": match(h).prompt_text()})
        if p == "/api/ideas":
            ideas = store.all_of("ideas")
            return self._send(200, {"ideas": list(ideas.values())[:5],
                                    "saved": list(store.all_of("fav_dest").values())})
        if m := re.fullmatch(rf"/api/jobs/({ID})", p):
            j = service.job(m.group(1))
            return self._send(200, j) if j else self._send(404, {"error": "Auftrag unbekannt (Neustart?)"})
        if p == "/api/notices":
            return self._send(200, {"notices": store.notices()})
        if p == "/api/costs":
            return self._send(200, {"recent": store.recent_costs(), "spent_24h": store.spent_since(24),
                                    "spent_30d": store.spent_since(24 * 30)})
        return self._send(404, {"error": "nicht gefunden"})

    # ---------- POST ----------
    def do_POST(self):
        p = urlparse(self.path).path
        if not self._gate("POST", p):
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._send(400, {"error": "ungültige Anfrage"})
        try:
            return self._post(p, body)
        except BadRequest as e:
            return self._send(400, {"error": str(e)})
        except ValidationError as e:
            err = e.errors()[0]
            loc = ".".join(str(x) for x in err.get("loc", ()))
            return self._send(400, {"error": f"Ungültige Angabe ({loc}): {err.get('msg')}"})
        except llm.LLMError as e:
            return self._send(502, {"error": str(e)})
        except KeyError:
            return self._send(404, {"error": "nicht gefunden"})

    def _post(self, p, body):
        if p == "/api/login":
            ip = self._ip()
            wait = auth.lock_seconds(ip)
            if wait:
                return self._send(429, {"error": f"Zu viele Fehlversuche. Bitte in {wait // 60 + 1} Min. nochmals versuchen."})
            if not auth.password_ok(str(body.get("password") or "")):
                auth.register_failure(ip)
                return self._send(401, {"error": "Passwort falsch."})
            auth.register_success(ip)
            return self._send(200, {"ok": True}, extra={
                "Set-Cookie": auth.cookie_header(auth.make_cookie(), auth.SESSION_DAYS * 86400)})
        if p in ("/api/logout", "/api/logout-all"):
            if p == "/api/logout-all":
                auth.logout_everywhere()
            return self._send(200, {"ok": True}, extra={"Set-Cookie": auth.cookie_header("", 0)})

        if p == "/api/household":
            h = Household.model_validate({**body.get("household", {}), "id": "main"})
            store.save_household(h)
            return self._send(200, {"household": h.model_dump(mode="json"),
                                    "matching": match(h).model_dump(mode="json"), "prompt": match(h).prompt_text()})
        if p == "/api/settings":
            allowed = {}
            if "model" in body:
                if body["model"] not in config.MODELS:
                    raise BadRequest("Unbekanntes Modell.")
                allowed["model"] = body["model"]
            if "effort" in body:
                if body["effort"] not in ("low", "medium", "high"):
                    raise BadRequest("Unbekannte Stufe.")
                allowed["effort"] = body["effort"]
            if "check_time" in body:
                if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(body["check_time"])):
                    raise BadRequest("Zeit als HH:MM angeben.")
                allowed["check_time"] = body["check_time"]
            if "prefer" in body:
                if body["prefer"] not in ("gratis", "claude"):
                    raise BadRequest("Unbekannte Reihenfolge.")
                allowed["prefer"] = body["prefer"]
            for k in ("auto_check", "web_search"):
                if k in body:
                    allowed[k] = bool(body[k])
            if "home_airport" in body:
                code = str(body["home_airport"]).strip().upper()
                if not re.fullmatch(r"[A-Z]{3}", code):
                    raise BadRequest("Flughafen als 3-Buchstaben-Code (z. B. ZRH).")
                allowed["home_airport"] = code
            store.save_settings(allowed)
            return self._send(200, info())
        if p == "/api/keys":
            prov = str(body.get("provider") or "anthropic")
            if prov not in store.KEY_PROVIDERS:
                raise BadRequest("Unbekannter Anbieter.")
            key = _text(body.get("key", body.get("anthropic")), 300)
            if re.search(r"\s", key):
                raise BadRequest("Der Schlüssel enthält Leerzeichen – bitte genau kopieren.")
            if key and prov == "anthropic" and not key.startswith("sk-"):
                raise BadRequest("Das sieht nicht wie ein Claude-Schlüssel aus (beginnt mit «sk-»).")
            if key and prov == "serpapi" and not re.fullmatch(r"[0-9a-fA-F]{32,128}", key):
                raise BadRequest("Das sieht nicht wie ein SerpApi-Schlüssel aus (lange Zahlen-/Buchstabenfolge).")
            if key and prov == "groq" and not key.startswith("gsk_"):
                raise BadRequest("Das sieht nicht wie ein Groq-Schlüssel aus (beginnt mit «gsk_»).")
            store.save_key(prov, key)
            return self._send(200, info())

        if p == "/api/destinations":
            start = _date(body.get("start"))
            days = _int(body.get("days"), 2, 30, "Tage")
            if start < date.today():
                raise BadRequest("Das Startdatum liegt in der Vergangenheit.")
            return self._send(200, {"job": service.start_destinations(start, days, _text(body.get("wish"), 400))})
        if p == "/api/trips":
            start = _date(body.get("start"))
            days = _int(body.get("days"), 2, 30, "Tage")
            dest = _text(body.get("destination"), 200)
            if not dest:
                raise BadRequest("Bitte ein Ziel angeben oder vorschlagen lassen.")
            return self._send(200, {"job": service.start_plan(dest, start, days, _text(body.get("wishes"), 800))})
        if p == "/api/fav-destination":
            item = body.get("idea") or {}
            if not isinstance(item, dict) or not item.get("id"):
                raise BadRequest("Vorschlag fehlt.")
            if body.get("remove"):
                store.delete("fav_dest", str(item["id"]))
            else:
                keep = {k: item.get(k) for k in ("id", "name", "country", "why", "highlights", "climate_text",
                                                 "est_total_chf", "typical_flight_hours")}
                store.put("fav_dest", str(item["id"]), keep)
            return self._send(200, {"saved": list(store.all_of("fav_dest").values())})

        if m := re.fullmatch(rf"/api/trips/({ID})/(\w[\w-]*(?:/apply)?)", p):
            return self._trip_post(m.group(1), m.group(2), body)
        if m := re.fullmatch(rf"/api/actions/({ID})", p):
            status = body.get("status")
            if status not in ("erledigt", "verworfen", "offen"):
                raise BadRequest("Status unbekannt.")
            ok = service.sm().set_action_status(m.group(1), status)
            return self._send(200 if ok else 404, {"ok": ok})
        if m := re.fullmatch(rf"/api/actions/({ID})/draft", p):
            act = next((a for a in service.sm().actions(status=None) if a["id"] == m.group(1)), None)
            if not act:
                raise KeyError
            d = drafting.write(act["kind"], act.get("payload", {}), _text(body.get("language"), 40),
                               trip_id=act["itinerary_id"])
            store.put("draft", f"{act['itinerary_id']}:{act['id']}", d.model_dump())
            return self._send(200, {"draft": d.model_dump()})
        if p == "/api/draft":
            kind = _text(body.get("kind"), 40) or "nachricht"
            d = drafting.write(kind, {k: _text(v, 200) for k, v in (body.get("payload") or {}).items()},
                               _text(body.get("language"), 40), _text(body.get("recipient"), 120),
                               _text(body.get("details"), 1200), trip_id=body.get("trip_id") or None)
            return self._send(200, {"draft": d.model_dump()})
        if p == "/api/vision":
            img = str(body.get("image") or "")
            mt = str(body.get("media_type") or "image/jpeg")
            res = vision.analyse(img, mt, _text(body.get("question"), 300), body.get("trip_id") or None)
            return self._send(200, {"result": res.model_dump()})
        if p == "/api/notices/read":
            store.mark_read()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "nicht gefunden"})

    def _trip_post(self, trip_id: str, action: str, body: dict):
        sm = service.sm()
        it = sm.get(trip_id)  # KeyError → 404
        if action == "weathercheck":
            return self._send(200, {"outcome": service.weather_check(trip_id), **trip_detail(trip_id)})
        if action == "transport":
            days = _int(body.get("days"), 1, 14, "Tage")
            out = service.report_transport(trip_id, str(body.get("segment_id") or ""), days,
                                           _text(body.get("reason"), 200))
            return self._send(200, {"outcome": out, **trip_detail(trip_id)})
        if action == "closure":
            out = service.report_closure(trip_id, str(body.get("activity_id") or ""), _text(body.get("reason"), 200))
            return self._send(200, {"outcome": out, **trip_detail(trip_id)})
        if action == "undo":
            res = sm.undo(str(body.get("change_id") or ""))
            ok = res.status.value == "angewendet"
            return self._send(200, {"ok": ok, "error": "" if ok else "Geht nicht mehr – der Plan wurde danach weiter "
                                    "geändert.", **trip_detail(trip_id)})
        if action == "status":
            out = service.change(trip_id, [SetTripStatus(status=body.get("status"))], "Status geändert")
            return self._send(200, {"outcome": out, **trip_detail(trip_id)})
        if action == "fixed":
            aid = str(body.get("activity_id") or "")
            out = service.change(trip_id, [SetFixed(activity_id=aid, fixed=bool(body.get("fixed")))],
                                 "Programmpunkt festgenagelt" if body.get("fixed") else "Programmpunkt freigegeben", [aid])
            return self._send(200, {"outcome": out, **trip_detail(trip_id)})
        if action == "confirm":
            op = SetConfirmed(target=body.get("target"), item_id=str(body.get("item_id") or ""),
                              confirmed=bool(body.get("confirmed")), reference=_text(body.get("reference"), 60) or None)
            out = service.change(trip_id, [op], "Als gebucht markiert" if op.confirmed else "Buchung zurückgesetzt")
            return self._send(200, {"outcome": out, **trip_detail(trip_id)})
        if action == "import":
            img = str(body.get("image") or "")
            text = _text(body.get("text"), 20000) if body.get("text") else ""
            if not img and len(text) < 20:
                raise BadRequest("Bitte ein Foto der Bestätigung wählen oder den Mailtext einfügen.")
            if img and str(body.get("media_type") or "image/jpeg") not in vision.MEDIA:
                raise BadRequest("Bitte ein Foto (JPEG, PNG oder WebP) wählen.")
            ext = bookings.extract(img or None, str(body.get("media_type") or "image/jpeg"), text, trip_id)
            if not ext.items:
                return self._send(200, {"preview": {"lines": [], "warnings": ["Keine Buchung erkannt."],
                                                    "problems": [], "can_apply": False, "summary": ext.summary}})
            return self._send(200, {"preview": bookings.preview(it, ext, service.local_now(it))})
        if action == "import/apply":
            data, ops = bookings.pending(str(body.get("token") or ""))
            if data["trip_id"] != trip_id:
                raise KeyError(trip_id)
            res = sm.apply(ChangeSet(itinerary_id=trip_id, base_version=data["version"], operations=ops,
                                     reason=f"Buchung importiert: {data['summary'][:120]}", actor="nutzer",
                                     acknowledged_locked=data.get("ack", [])),
                           store.household().constraints.model_copy(update={"budget_total_max": None}),
                           service.local_now(it))
            store.delete("import", str(body.get("token")))
            ok = res.status.value == "angewendet"
            err = "" if ok else ("Der Plan wurde inzwischen geändert – bitte nochmals importieren."
                                 if res.status.value == "konflikt" else "; ".join(p.message for p in res.problems))
            return self._send(200, {"ok": ok, "error": err, **trip_detail(trip_id)})
        if action == "prices":
            target, item_id = body.get("target"), str(body.get("item_id") or "")
            try:
                if target == "verbindung":
                    seg = next(t for t in it.transports if t.id == item_id)
                    res = prices.flights(seg, store.household().constraints.max_stops, trip_id)
                elif target == "unterkunft":
                    stay = next(x for x in it.stays if x.id == item_id)
                    night = match(store.household()).budget_per_night
                    res = prices.hotels(stay, float(night) if night else None, trip_id)
                else:
                    raise BadRequest("Unbekanntes Ziel.")
            except StopIteration:
                raise KeyError(item_id)
            except prices.PriceError as e:
                return self._send(502, {"error": str(e)})
            res["token"] = prices.remember(target, trip_id, item_id, res)
            res["month"] = prices.month_usage()
            return self._send(200, res)
        if action == "prices/apply":
            data, opt = prices.picked(str(body.get("token") or ""), _int(body.get("index"), 0, 50, "Angebot"))
            if data["trip_id"] != trip_id:
                raise KeyError(trip_id)
            if data["kind"] == "verbindung":
                op = UpdateTransport(segment_id=data["item_id"], depart=datetime.fromisoformat(opt["depart"]),
                                     arrive=datetime.fromisoformat(opt["arrive"]), carrier=opt["carrier"] or None,
                                     number=opt["number"] or None, via=opt["via"], cost=round(opt["price"], 2),
                                     search_url=data.get("url"), note=f"Preis laut Google Flights am {date.today():%d.%m.%Y}")
                reason = f"Flug aus echten Preisen übernommen ({opt['carrier']}, CHF {opt['price']:.0f})"
            else:
                total = opt["total"] or 0
                photos.set_stay_photo(trip_id, data["item_id"], opt.get("photo"))
                op = UpdateStay(stay_id=data["item_id"], name=opt["name"], cost=round(total, 2),
                                search_url=opt.get("link"),
                                note=f"CHF {opt['night']:.0f}/Nacht laut Google Hotels am {date.today():%d.%m.%Y}")
                reason = f"Unterkunft aus echten Preisen übernommen ({opt['name']})"
            out = service.change(trip_id, [op], reason)
            return self._send(200, {"outcome": out, **trip_detail(trip_id)})
        if action == "favorite":
            if store.get("fav", trip_id):
                store.delete("fav", trip_id)
            else:
                store.put("fav", trip_id, {"saved_at": datetime.now().isoformat(), "title": it.title})
            return self._send(200, trip_detail(trip_id))
        return self._send(404, {"error": "nicht gefunden"})

    # ---------- DELETE ----------
    def do_DELETE(self):
        p = urlparse(self.path).path
        if not self._gate("DELETE", p):
            return
        if m := re.fullmatch(rf"/api/trips/({ID})", p):
            ok = service.sm().delete(m.group(1))
            store.delete("fav", m.group(1))
            return self._send(200 if ok else 404, {"ok": ok})
        return self._send(404, {"error": "nicht gefunden"})


def main():
    config.ensure_dirs()
    problem = auth.password_problem()
    if problem:
        print(f"ACHTUNG: {problem} Die App bleibt gesperrt, bis das behoben ist.", flush=True)
    if service.sm().delete("demo-lissabon"):  # Überbleibsel der Testversion 0.2
        print("Alte Demo-Reise entfernt.", flush=True)
    service.start_scheduler()
    srv = ThreadingHTTPServer(("0.0.0.0", config.PORT), Handler)
    print(f"Kompass {config.APP_VERSION} ({config.APP_BUILD}) läuft auf Port {config.PORT} "
          f"(Testmodus: {config.FAKE_MODE}, KI: {'ja' if llm.available() else 'kein Schlüssel'})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
