"""Einfache JSON-Ablage in SQLite (gleiche Datei wie die Reisepläne).

Haushalt, Einstellungen, Meldungen, Kosten, Favoriten, Entwürfe, Zielvorschläge.
Reisepläne selbst verwaltet ausschliesslich der StatusManager.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta

from . import config
from .models import Household, UserProfile
from .models.common import new_id

_lock = threading.RLock()  # verschachtelt nutzbar (db() wird unter der Sperre aufgerufen)
_db: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, updated TEXT NOT NULL,
                               PRIMARY KEY (kind, id));
CREATE TABLE IF NOT EXISTS costs (ts TEXT NOT NULL, purpose TEXT NOT NULL, model TEXT, usd REAL NOT NULL,
                                  input_tokens INTEGER, output_tokens INTEGER, trip_id TEXT);
CREATE TABLE IF NOT EXISTS notices (id TEXT PRIMARY KEY, ts TEXT NOT NULL, trip_id TEXT, kind TEXT NOT NULL,
                                    title TEXT NOT NULL, body TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0);
"""


def db() -> sqlite3.Connection:
    global _db
    with _lock:
        if _db is None:
            config.ensure_dirs()
            _db = sqlite3.connect(str(config.DB_PATH), isolation_level=None, check_same_thread=False)
            _db.execute("PRAGMA journal_mode = WAL")
            _db.executescript(SCHEMA)
        return _db


def reset_connection() -> None:
    """Nur für Tests: Verbindung schliessen (anderer DATA_DIR)."""
    global _db
    with _lock:
        if _db is not None:
            _db.close()
        _db = None


# --- Schlüssel/Werte ------------------------------------------------------------------------

def get(kind: str, id_: str, default=None):
    row = db().execute("SELECT data FROM kv WHERE kind = ? AND id = ?", (kind, id_)).fetchone()
    return json.loads(row[0]) if row else default


def put(kind: str, id_: str, data) -> None:
    with _lock:
        db().execute("INSERT INTO kv (kind, id, data, updated) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(kind, id) DO UPDATE SET data = excluded.data, updated = excluded.updated",
                     (kind, id_, json.dumps(data, ensure_ascii=False, default=str), datetime.now().isoformat()))


def delete(kind: str, id_: str) -> None:
    with _lock:
        db().execute("DELETE FROM kv WHERE kind = ? AND id = ?", (kind, id_))


def all_of(kind: str) -> dict:
    rows = db().execute("SELECT id, data FROM kv WHERE kind = ? ORDER BY updated DESC", (kind,)).fetchall()
    return {r[0]: json.loads(r[1]) for r in rows}


# --- Haushalt -------------------------------------------------------------------------------

def household() -> Household:
    data = get("household", "main")
    if data:
        return Household.model_validate(data)
    return Household(id="main", profiles=[UserProfile(name="Ich", role="haupt"),
                                          UserProfile(name="Partnerin", role="partnerin")])


def save_household(h: Household) -> None:
    put("household", "main", h.model_dump(mode="json"))


# --- Einstellungen und Schlüssel --------------------------------------------------------------

DEFAULT_SETTINGS = {"model": config.MODEL, "effort": config.EFFORT, "check_time": config.CHECK_TIME,
                    "auto_check": True, "web_search": False, "home_airport": config.HOME_AIRPORT,
                    "prefer": "gratis"}  # gratis = Gemini/Groq zuerst, Claude nur als Reserve


def settings() -> dict:
    return {**DEFAULT_SETTINGS, **(get("settings", "main") or {})}


def save_settings(changes: dict) -> dict:
    s = settings()
    s.update(changes)
    put("settings", "main", {k: v for k, v in s.items() if k in DEFAULT_SETTINGS})
    return settings()


def _keys_path():
    return config.DATA_DIR / "keys.json"


KEY_PROVIDERS = ("anthropic", "gemini", "groq", "serpapi")


def _env_key(provider: str) -> str:
    if provider == "anthropic":
        return config.ANTHROPIC_API_KEY
    if provider == "serpapi":
        return config.SERPAPI_API_KEY
    return config.FREE_PROVIDERS[provider]["key"]


def _app_keys() -> dict:
    try:
        return json.loads(_keys_path().read_text())
    except (OSError, ValueError):
        return {}


def key(provider: str) -> str:
    """Schlüssel eines Anbieters: in der App hinterlegt gilt vor einstellungen.env."""
    return _app_keys().get(provider, "") or _env_key(provider)


def anthropic_key() -> str:
    return key("anthropic")


def save_key(provider: str, value: str) -> None:
    if provider not in KEY_PROVIDERS:
        raise ValueError(provider)
    data = _app_keys()
    data[provider] = value.strip()
    p = _keys_path()
    p.write_text(json.dumps(data))
    os.chmod(p, 0o600)


def save_anthropic_key(value: str) -> None:
    save_key("anthropic", value)


def key_info() -> dict:
    """Nur die letzten 4 Zeichen – den Schlüssel selbst nie ausliefern."""
    app = _app_keys()
    out = {}
    for prov in KEY_PROVIDERS:
        k = key(prov)
        out[prov] = {"set": bool(k), "hint": k[-4:] if k else "",
                     "source": "app" if app.get(prov) else ("env" if k else "")}
    return out


# --- Kosten ---------------------------------------------------------------------------------

def add_cost(purpose: str, model: str, usd: float, tin: int, tout: int, trip_id: str | None = None) -> None:
    with _lock:
        db().execute("INSERT INTO costs VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (datetime.now().isoformat(), purpose, model, usd, tin, tout, trip_id))


def spent_since(hours: float) -> float:
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    (v,) = db().execute("SELECT COALESCE(SUM(usd), 0) FROM costs WHERE ts >= ?", (since,)).fetchone()
    return float(v)


def spent_for_trip(trip_id: str) -> float:
    (v,) = db().execute("SELECT COALESCE(SUM(usd), 0) FROM costs WHERE trip_id = ?", (trip_id,)).fetchone()
    return float(v)


def recent_costs(limit: int = 30) -> list[dict]:
    rows = db().execute("SELECT ts, purpose, model, usd FROM costs ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [{"ts": r[0], "purpose": r[1], "model": r[2], "usd": r[3]} for r in rows]


# --- Meldungen ------------------------------------------------------------------------------

def notify(kind: str, title: str, body: str, trip_id: str | None = None) -> str:
    nid = new_id()
    with _lock:
        db().execute("INSERT INTO notices (id, ts, trip_id, kind, title, body) VALUES (?, ?, ?, ?, ?, ?)",
                     (nid, datetime.now().isoformat(), trip_id, kind, title, body))
    print(f"[Meldung] {title}: {body[:160]}", flush=True)
    return nid


def notices(limit: int = 50) -> list[dict]:
    rows = db().execute("SELECT id, ts, trip_id, kind, title, body, read FROM notices ORDER BY ts DESC LIMIT ?",
                        (limit,)).fetchall()
    return [{"id": r[0], "ts": r[1], "trip_id": r[2], "kind": r[3], "title": r[4], "body": r[5], "read": bool(r[6])}
            for r in rows]


def mark_read() -> None:
    with _lock:
        db().execute("UPDATE notices SET read = 1 WHERE read = 0")


def unread() -> int:
    (n,) = db().execute("SELECT COUNT(*) FROM notices WHERE read = 0").fetchone()
    return int(n)
