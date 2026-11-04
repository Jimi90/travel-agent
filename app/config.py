"""Zentrale Einstellungen. Alles kommt aus Umgebungsvariablen (einstellungen.env)."""
import os
from pathlib import Path


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


DATA_DIR = Path(_env("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "kompass.db"
PORT = int(_env("PORT", "8000"))
APP_VERSION = "1.8.4"  # von Hand erhöhen
APP_BUILD = _env("APP_BUILD", "entwicklung")[:7]  # von GitHub gesetzt
APP_PASSWORD = _env("APP_PASSWORD", "")  # Pflicht, mindestens 16 Zeichen

# Testmodus: simuliert KI und Wetter (keine Kosten, kein Netz)
FAKE_MODE = _env("FAKE_MODE", "0") == "1"

# --- Claude (Planung, Bilderkennung, Entwürfe) ---
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")
# Standardmodell. Günstiger: claude-sonnet-5-5 oder claude-haiku-5-5 (in den Einstellungen umstellbar).
MODEL = _env("MODEL", "claude-opus-5-5")
EFFORT = _env("EFFORT", "medium")  # low | medium | high
# Preise in USD pro 1 Mio. Tokens (Eingabe, Ausgabe) – nur für die Kostenanzeige.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
MODELS = list(PRICES)
WEB_SEARCH_USD = 0.01  # pro Websuche (Schätzung)

# --- Gratis-KI (OpenAI-kompatible Schnittstellen, wie in Hörwerk) ---
# Gemini: Schlüssel bei aistudio.google.com («Get API key»). Gratis-Inhalte darf Google zur Verbesserung nutzen.
# Groq:   Schlüssel bei console.groq.com («API Keys»). Sehr schnell, aber kleines Minutenkontingent.
FREE_PROVIDERS = {
    "gemini": {"label": "Gemini", "key": _env("GEMINI_API_KEY", ""),
               "base_url": _env("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"),
               "model": _env("GEMINI_MODEL", "gemini-3.8-flash"), "vision": True, "tpm": 0,
               "models": ["gemini-3.8-flash", "gemini-3.5-flash-lite"]},
    "groq": {"label": "Groq", "key": _env("GROQ_API_KEY", ""),
             "base_url": _env("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
             "model": _env("GROQ_MODEL", "openai/gpt-oss-120b"), "vision": False,
             # Gratis-Konto: ca. 8000 Tokens pro Minute (Frage + Antwort zusammen) – zu klein für ganze Reisepläne
             "tpm": int(_env("GROQ_TPM", "8000")), "models": ["openai/gpt-oss-120b"]},
}
# Höchstens so viel USD pro 24 Stunden für KI-Aufrufe
DAILY_BUDGET_USD = float(_env("DAILY_BUDGET_USD", "3"))

# --- Echte Preise (SerpApi: Google Flights / Google Hotels), Gratis-Tarif 250 Suchen/Monat ---
SERPAPI_API_KEY = _env("SERPAPI_API_KEY", "")

# --- Morgen-Wettercheck ---
CHECK_TIME = _env("CHECK_TIME", "06:30")  # Ortszeit am Aufenthaltsort
HOME_AIRPORT = _env("HOME_AIRPORT", "ZRH")


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
