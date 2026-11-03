"""Fotos zu Orten, Programmpunkten, Zielvorschlägen und Hotels.

Quellen:
- Wikipedia (gratis, ohne Schlüssel): Seitenbild des besten Suchtreffers, nur frei lizenzierte Bilder
  (`pilicense=free`; deutsche Wikipedia zuerst, sie erlaubt ohnehin nur freie Bilder).
- Google Hotels (über SerpApi), wenn «Echte Preise» Bilder mitliefert.

Der Server lädt die Bilder selbst und liefert sie über /api/photo/<id> aus: So sehen fremde Seiten
die Adresse nicht, und die Sicherheitsregeln (CSP img-src 'self') bleiben streng.
Nur Bilder von bekannten Bild-Servern, höchstens 2 MB, nur JPEG/PNG/WebP.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from . import config, store

UA = f"Kompass/{config.APP_VERSION} (privater Reiseplaner)"
WIKIS = ("https://de.wikipedia.org/w/api.php", "https://en.wikipedia.org/w/api.php")
ALLOWED_HOSTS = (".wikimedia.org", ".googleusercontent.com", ".gstatic.com")
TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
MAX_BYTES = 2_000_000
# Programmpunkte, zu denen ein Wikipedia-Bild nur Zufall wäre
GENERIC = re.compile(r"^(abendessen|mittagessen|frühstück|fruhstuck|ankunft|abreise|anreise|freier tag|pause|"
                     r"check-?in|check-?out|transfer|flug|fähre|spaziergang|einkaufen|ruhetag|strandtag)\b", re.I)
STOP = {"und", "der", "die", "das", "mit", "von", "im", "in", "am", "an", "zum", "zur", "the", "of", "and", "de", "do",
        "da", "la", "le", "el", "tour", "besuch", "ausflug", "wanderung", "spaziergang", "fahrt", "abend", "morgen"}

_lock = threading.Lock()
_running: set[str] = set()


def _dir():
    d = config.DATA_DIR / "photos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().casefold()


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", _fold(s)) if w not in STOP}


def _get_json(url: str, params: dict) -> dict:
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _allowed(url: str) -> bool:
    u = urllib.parse.urlparse(url or "")
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host.endswith(h) for h in ALLOWED_HOSTS)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Weiterleitungen nur zu erlaubten Bild-Servern (nie ins Heimnetz o. ä.)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed(newurl):
            raise urllib.error.URLError("Weiterleitung zu fremdem Server")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect)


def download(url: str, source: str, title: str = "", page: str = "") -> str | None:
    """Bild holen und ablegen → Foto-Kennung (oder None). Gleiche Adresse = gleiche Kennung."""
    if not _allowed(url):
        return None
    pid = hashlib.sha256(url.encode()).hexdigest()[:24]
    if store.get("photo", pid):
        return pid
    if config.FAKE_MODE:
        _dir().joinpath(f"{pid}.svg").write_text(_fake_svg(title or url))
        store.put("photo", pid, {"file": f"{pid}.svg", "type": "image/svg+xml", "source": source, "title": title,
                                 "page": page})
        return pid
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with _opener.open(req, timeout=20) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype not in TYPES or not _allowed(r.geturl()):
                return None
            data = r.read(MAX_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    if len(data) > MAX_BYTES:
        return None
    name = f"{pid}.{TYPES[ctype]}"
    _dir().joinpath(name).write_bytes(data)
    store.put("photo", pid, {"file": name, "type": ctype, "source": source, "title": title, "page": page})
    return pid


def get(pid: str) -> tuple[bytes, str, dict] | None:
    meta = store.get("photo", pid) if re.fullmatch(r"[0-9a-f]{24}", pid or "") else None
    if not meta:
        return None
    f = _dir() / meta["file"]
    if not f.exists():
        return None
    return f.read_bytes(), meta["type"], meta


# --- Wikipedia --------------------------------------------------------------------------------

def parse_wiki(data: dict) -> dict | None:
    """MediaWiki-Antwort (formatversion=2, generator=search, prop=pageimages|info) → bestes Bild."""
    pages = (data.get("query") or {}).get("pages") or []
    pages = sorted(pages, key=lambda p: p.get("index", 99))
    for p in pages:
        th = p.get("thumbnail") or {}
        if th.get("source"):
            return {"url": th["source"], "title": p.get("title", ""), "page": p.get("fullurl", "")}
    return None


def wiki_photo(query: str, must_match: str = "") -> str | None:
    """Foto zum Suchbegriff; must_match: Wörter, von denen der Seitentitel mindestens eins enthalten muss."""
    key = "wiki:" + _fold(query).strip()
    cached = store.get("photo_q", key)
    if cached is not None:
        return cached.get("id")
    pid = None
    need = _words(must_match)
    for api in WIKIS:
        if config.FAKE_MODE:
            hit = {"url": f"https://upload.wikimedia.org/fake/{urllib.parse.quote(query)}.jpg", "title": query,
                   "page": ""}
        else:
            try:
                data = _get_json(api, {"action": "query", "format": "json", "formatversion": 2, "generator": "search",
                                       "gsrsearch": query, "gsrlimit": 3, "gsrnamespace": 0, "prop": "pageimages|info",
                                       "inprop": "url", "piprop": "thumbnail", "pithumbsize": 800, "pilicense": "free"})
            except (urllib.error.URLError, TimeoutError, ValueError):
                return None  # später nochmals versuchen (nichts merken)
            hit = parse_wiki(data)
        if hit and (not need or need & _words(hit["title"])):
            pid = download(hit["url"], "Wikipedia", hit["title"], hit["page"])
            if pid:
                break
    store.put("photo_q", key, {"id": pid})
    return pid


# --- Fotos einer Reise -------------------------------------------------------------------------

def trip_photos(trip_id: str) -> dict:
    return store.get("trip_photos", trip_id) or {"places": {}, "activities": {}, "stays": {}}


def activity_key(title: str) -> str:
    return _fold(title).strip()


def fill_trip(itin) -> None:
    """Fehlende Fotos einer Reise im Hintergrund holen (Orte, Programmpunkte)."""
    with _lock:
        if itin.id in _running:
            return
        _running.add(itin.id)

    def run():
        try:
            m = trip_photos(itin.id)
            for d in itin.days:
                b = d.base
                if b.name not in m["places"]:
                    m["places"][b.name] = wiki_photo(b.name, must_match=b.name)
                    store.put("trip_photos", itin.id, m)
                for a in d.activities:
                    k = activity_key(a.title)
                    if k in m["activities"]:
                        continue
                    if GENERIC.match(a.title.strip()) or not _words(a.title):
                        m["activities"][k] = None
                    else:
                        m["activities"][k] = wiki_photo(f"{a.title} {b.name}", must_match=a.title)
                    store.put("trip_photos", itin.id, m)
        except Exception as e:  # Fotos sind nett, aber nie wichtig genug für einen Absturz
            print(f"[Fotos] {itin.id}: {type(e).__name__}: {e}", flush=True)
        finally:
            with _lock:
                _running.discard(itin.id)

    if config.FAKE_MODE and getattr(fill_trip, "sync", False):
        run()
    else:
        threading.Thread(target=run, daemon=True, name=f"fotos-{itin.id}").start()


def missing(itin) -> bool:
    m = trip_photos(itin.id)
    return any(d.base.name not in m["places"] or any(activity_key(a.title) not in m["activities"]
                                                       for a in d.activities) for d in itin.days)


def set_stay_photo(trip_id: str, stay_id: str, pid: str | None) -> None:
    m = trip_photos(trip_id)
    m["stays"][stay_id] = pid
    store.put("trip_photos", trip_id, m)


def _fake_svg(seed: str) -> str:
    h = int(hashlib.md5(seed.encode()).hexdigest()[:6], 16)
    c1, c2 = f"hsl({h % 360},55%,55%)", f"hsl({(h // 7) % 360},60%,35%)"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 260"><defs><linearGradient id="g" x1="0" '
            f'y1="0" x2="1" y2="1"><stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/>'
            f'</linearGradient></defs><rect width="400" height="260" fill="url(#g)"/>'
            f'<circle cx="300" cy="80" r="34" fill="#fff" opacity=".5"/>'
            f'<path d="M0 260 L120 140 L200 210 L270 160 L400 260Z" fill="#fff" opacity=".35"/></svg>')
