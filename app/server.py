"""Webserver von Kompass (Standardbibliothek, wie Hörwerk).

Jede Anfrage läuft durch ``_gate()``: ohne gültige Sitzung nur /login, /health, Manifest und Symbole.
Schritt 2 (Testversion): Demo-Reise anzeigen, Wettercheck simulieren, Rückgängig, Demo zurücksetzen.
"""
from __future__ import annotations

import json
import threading
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import auth, config, demo
from .core.status_manager import StatusManager
from .models import ChangeStatus
from .planning.alternatives import AlternativeActivitiesModule
from .planning.replanning import AutonomousRePlanningModule

WEB = Path(__file__).parent / "web"
MAX_BODY = 64 * 1024
STATIC = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/favicon.ico": ("icon.svg", "image/svg+xml"),
}
PUBLIC_GET = {"/health", "/login", *STATIC}

_lock = threading.Lock()
_sm: StatusManager | None = None


def status() -> StatusManager:
    global _sm
    with _lock:
        if _sm is None:
            _sm = StatusManager(config.DB_PATH)
            try:
                _sm.get(demo.DEMO_ID)
            except KeyError:
                _sm.create(demo.itinerary())
        return _sm


def reset_demo() -> None:
    global _sm
    with _lock:
        if _sm is not None:
            _sm.close()
            _sm = None
        for suffix in ("", "-wal", "-shm"):
            Path(str(config.DB_PATH) + suffix).unlink(missing_ok=True)
    status()


def state() -> dict:
    sm = status()
    itin = sm.get(demo.DEMO_ID)
    now = demo.demo_clock(itin)
    plan = itin.model_dump(mode="json")
    for day, raw in zip(itin.days, plan["days"]):
        for a, ra in zip(day.activities, raw["activities"]):
            ra["locked"] = a.locked(now)
    hist = [h.model_dump(mode="json") for h in sm.history(itin.id)]
    for h in hist:
        h["can_undo"] = h["undone_by"] is None and h["to_version"] == itin.version and h["operations"] != []
    return {
        "version": config.APP_VERSION, "build": config.APP_BUILD, "plan": plan,
        "total": str(itin.total_cost()), "now": now.isoformat(),
        "history": list(reversed(hist)), "actions": sm.pending_actions(), "scenarios": demo.SCENARIOS,
    }


def run_check(scenario: str, pregnant: bool, dry_next_day: bool) -> dict:
    sm = status()
    itin = sm.get(demo.DEMO_ID)
    hh = demo.household(pregnant)
    mod = AutonomousRePlanningModule(
        sm, AlternativeActivitiesModule(demo.CATALOG, hh), hh,
        forecast_lookup=demo.lookup_dry_next_day(itin) if dry_next_day else None,
        clock=lambda: demo.demo_clock(itin),
    )
    ev = demo.event(itin, scenario)
    out = mod.handle(ev)
    print(f"[Umplanung] {ev.description}: {out.status.value}, Versuche {out.attempts}", flush=True)
    return {
        "status": out.status.value, "applied": out.status is ChangeStatus.APPLIED,
        "message": out.message(), "needs_user": out.needs_user,
        "decisions": [d.model_dump(mode="json") for d in out.decisions],
        "problems": [p.model_dump(mode="json") for p in (out.result.problems if out.result else [])],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "Kompass/0"
    protocol_version = "HTTP/1.1"

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
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
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
        if method == "POST":
            if self.headers.get(auth.CSRF_HEADER) != "1":
                self._send(403, {"error": "Anfrage nicht erlaubt."})
                return False
            if int(self.headers.get("Content-Length") or 0) > MAX_BODY:
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

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        p = urlparse(self.path).path
        if not self._gate("GET", p):
            return
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
        if p == "/api/state":
            return self._send(200, state())
        return self._send(404, {"error": "nicht gefunden"})

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
        if p == "/api/logout":
            return self._send(200, {"ok": True}, extra={"Set-Cookie": auth.cookie_header("", 0)})
        if p == "/api/logout-all":
            auth.logout_everywhere()
            return self._send(200, {"ok": True}, extra={"Set-Cookie": auth.cookie_header("", 0)})
        if p == "/api/demo/check":
            scenario = str(body.get("scenario") or "")
            if scenario not in demo.SCENARIOS:
                return self._send(400, {"error": "unbekanntes Wetter"})
            out = run_check(scenario, bool(body.get("pregnant")), bool(body.get("dry_next_day")))
            return self._send(200, {"outcome": out, "state": state()})
        if p == "/api/undo":
            try:
                res = status().undo(str(body.get("change_id") or ""))
            except KeyError:
                return self._send(404, {"error": "Änderung nicht gefunden"})
            ok = res.status is ChangeStatus.APPLIED
            return self._send(200 if ok else 409, {"ok": ok, "error": "" if ok else "Geht nicht mehr – der Plan wurde danach weiter geändert.", "state": state()})
        if p == "/api/demo/reset":
            reset_demo()
            return self._send(200, {"state": state()})
        return self._send(404, {"error": "nicht gefunden"})


def main():
    config.ensure_dirs()
    problem = auth.password_problem()
    if problem:
        print(f"ACHTUNG: {problem} Die App bleibt gesperrt, bis das behoben ist.", flush=True)
    status()
    srv = ThreadingHTTPServer(("0.0.0.0", config.PORT), Handler)
    print(f"Kompass {config.APP_VERSION} ({config.APP_BUILD}) läuft auf Port {config.PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
