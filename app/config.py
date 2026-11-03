"""Zentrale Einstellungen. Alles kommt aus Umgebungsvariablen (einstellungen.env)."""
import os
from pathlib import Path


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


DATA_DIR = Path(_env("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "reise.db"
PORT = int(_env("PORT", "8000"))
APP_VERSION = "0.2.0"  # von Hand erhöhen
APP_BUILD = _env("APP_BUILD", "entwicklung")[:7]  # von GitHub gesetzt
APP_PASSWORD = _env("APP_PASSWORD", "")  # Pflicht, mindestens 16 Zeichen


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
