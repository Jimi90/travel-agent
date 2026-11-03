"""Anmeldung für Kompass – gebaut für den Betrieb im offenen Internet (Tailscale Funnel).

Schutzschichten:
- Ohne starkes Passwort (APP_PASSWORD, mind. 16 Zeichen) startet die App gesperrt («fail closed»).
- Sitzung als signiertes Cookie (HMAC-SHA256), 90 Tage gültig, HttpOnly + Secure + SameSite=Lax.
  Der Schlüssel hängt am Passwort und an einem zufälligen Geheimnis in data/: Passwort ändern oder
  «Überall abmelden» macht alle alten Sitzungen ungültig.
- Sperre bei Fehlversuchen: 5 Fehler einer Adresse → 15 Min. gesperrt; 20 Fehler insgesamt pro Stunde
  → Anmeldung für alle 60 Min. gesperrt (gegen verteiltes Durchprobieren). Jeder Fehlversuch wartet 1.5 s.
- Schutz gegen fremde Webseiten (CSRF): Änderungen nur mit eigenem Header «X-Kompass».
"""
import hashlib
import hmac
import secrets
import threading
import time

from . import config

COOKIE = "kompass_sitzung"
SESSION_DAYS = 90
MIN_PW_LEN = 16
IP_MAX_FAILS, IP_WINDOW, IP_LOCK = 5, 15 * 60, 15 * 60
GLOBAL_MAX_FAILS, GLOBAL_WINDOW, GLOBAL_LOCK = 20, 60 * 60, 60 * 60
CSRF_HEADER = "X-Kompass"

_lock = threading.Lock()
_fails = {}            # Adresse → [Zeitpunkte]
_locked_until = {}     # Adresse → Zeitpunkt
_global_fails = []     # [Zeitpunkte]
_global_locked_until = 0.0


def password_problem():
    """Grund, warum die Anmeldung nicht sicher ist – oder '' wenn alles ok."""
    pw = config.APP_PASSWORD
    if not pw:
        return "APP_PASSWORD fehlt in einstellungen.env."
    if len(pw) < MIN_PW_LEN:
        return f"APP_PASSWORD ist zu kurz (mindestens {MIN_PW_LEN} Zeichen)."
    if pw.lower() in ("bitte-aendern-bitte-aendern", "passwortpasswort"):
        return "APP_PASSWORD ist noch der Beispielwert."
    return ""


def _secret():
    p = config.DATA_DIR / "geheimnis.key"
    if not p.exists():
        p.write_text(secrets.token_hex(32))
        p.chmod(0o600)
    return p.read_text().strip()


def _key():
    return hashlib.sha256(f"kompass|{_secret()}|{config.APP_PASSWORD}".encode()).digest()


def _sign(payload):
    return hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()


def make_cookie():
    exp = int(time.time()) + SESSION_DAYS * 86400
    payload = f"{exp}.{secrets.token_hex(8)}"
    return f"{payload}.{_sign(payload)}"


def cookie_ok(value):
    if password_problem() or not value:
        return False
    try:
        exp, nonce, sig = value.split(".")
        payload = f"{exp}.{nonce}"
        return int(exp) > time.time() and hmac.compare_digest(sig, _sign(payload))
    except (ValueError, TypeError):
        return False


def logout_everywhere():
    """Neues Geheimnis → alle bisherigen Sitzungen sind ungültig."""
    (config.DATA_DIR / "geheimnis.key").write_text(secrets.token_hex(32))


def password_ok(pw):
    if password_problem():
        return False
    a = hashlib.sha256(pw.encode()).digest()
    b = hashlib.sha256(config.APP_PASSWORD.encode()).digest()
    return hmac.compare_digest(a, b)


def lock_seconds(ip):
    """Sekunden bis zur nächsten erlaubten Anmeldung (0 = jetzt erlaubt)."""
    now = time.time()
    with _lock:
        wait = max(_locked_until.get(ip, 0), _global_locked_until) - now
        return int(wait) + 1 if wait > 0 else 0


def register_failure(ip):
    global _global_locked_until
    now = time.time()
    with _lock:
        recent = [t for t in _fails.get(ip, []) if now - t < IP_WINDOW] + [now]
        _fails[ip] = recent
        if len(recent) >= IP_MAX_FAILS:
            _locked_until[ip] = now + IP_LOCK
            _fails.pop(ip, None)
        _global_fails[:] = [t for t in _global_fails if now - t < GLOBAL_WINDOW] + [now]
        if len(_global_fails) >= GLOBAL_MAX_FAILS:
            _global_locked_until = now + GLOBAL_LOCK
            _global_fails.clear()
    print(f"[Anmeldung] Fehlversuch von {ip}", flush=True)
    time.sleep(1.5)  # bremst automatisches Durchprobieren


def register_success(ip):
    with _lock:
        _fails.pop(ip, None)


def cookie_header(value, max_age):
    return (f"{COOKIE}={value}; Max-Age={max_age}; Path=/; HttpOnly; Secure; SameSite=Lax")


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'; object-src 'none'"
    ),
}
