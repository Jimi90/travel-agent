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
ALLOWED_HOSTS = (".wikimedia.org", ".googleusercontent.com", ".gstatic.com", ".serpapi.com")
TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
MAX_BYTES = 2_000_000
# Programmpunkte, zu denen ein Wikipedia-Bild nur Zufall wäre
GENERIC = re.compile(r"^(abendessen|mittagessen|frühstück|fruhstuck|ankunft|abreise|anreise|freier tag|pause|"
                     r"check-?in|check-?out|transfer|flug|fähre|spaziergang|einkaufen|ruhetag|strandtag)\b", re.I)
STOP = {"und", "der", "die", "das", "mit", "von", "im", "in", "am", "an", "zum", "zur", "the", "of", "and", "de", "do",
        "da", "la", "le", "el", "tour", "besuch", "ausflug", "wanderung", "spaziergang", "fahrt", "abend", "morgen"}

# Bilder, die nie «das Ziel» zeigen: Wappen, Siegel, Flaggen, Logos, Karten (kamen bei Stadtartikeln oft als Seitenbild)
BAD_IMAGE = re.compile(r"coat[_ -]?of[_ -]?arms|wappen|blason|escudo|stemma|(^|[^a-z])seal([^a-z]|$)|flag|flagge|bandera|"
                       r"logo|locator|location[_ -]?map|(^|[^a-z])map([^a-z]|$)|karte|emblem|\.svg", re.I)

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
    return u.scheme == "https" and any(host.endswith(h) or host == h[1:] for h in ALLOWED_HOSTS)


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

def parse_wiki_all(data: dict) -> list[dict]:
    """MediaWiki-Antwort (formatversion=2, prop=pageimages|info) → alle Seiten mit Bild, beste zuerst."""
    pages = sorted((data.get("query") or {}).get("pages") or [], key=lambda p: p.get("index", 99))
    return [{"url": p["thumbnail"]["source"], "title": p.get("title", ""), "page": p.get("fullurl", "")}
            for p in pages if (p.get("thumbnail") or {}).get("source")]


def bad_image(url: str, title: str = "") -> bool:
    """Wappen, Siegel, Flaggen, Logos und Karten sind kein Foto des Ziels."""
    name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
    return bool(BAD_IMAGE.search(name) or BAD_IMAGE.search(title or ""))


def parse_wiki(data: dict) -> dict | None:
    hits = parse_wiki_all(data)
    return hits[0] if hits else None


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
            hits = [{"url": f"https://upload.wikimedia.org/fake/{urllib.parse.quote(query)}.jpg", "title": query,
                     "page": ""}]
        else:
            try:
                data = _get_json(api, {"action": "query", "format": "json", "formatversion": 2, "generator": "search",
                                       "gsrsearch": query, "gsrlimit": 6, "gsrnamespace": 0, "prop": "pageimages|info",
                                       "inprop": "url", "piprop": "thumbnail", "pithumbsize": 800, "pilicense": "free"})
            except (urllib.error.URLError, TimeoutError, ValueError):
                return None  # später nochmals versuchen (nichts merken)
            hits = parse_wiki_all(data)
        # alle Treffer prüfen, nicht nur den ersten (oft ist das der Artikel über die Stadt)
        hit = next((h for h in hits if (not need or need & _words(h["title"])) and not bad_image(h["url"], h["title"])),
                   None)
        if hit:
            pid = download(hit["url"], "Wikipedia", hit["title"], hit["page"])
            if pid:
                break
    store.put("photo_q", key, {"id": pid})
    return pid


def wiki_title_photo(title: str) -> str | None:
    """Foto eines bestimmten Wikipedia-Artikels (Titel von der KI), deutsch zuerst, Weiterleitungen folgen."""
    key = "title:" + _fold(title).strip()
    cached = store.get("photo_q", key)
    if cached is not None:
        return cached.get("id")
    pid = None
    for api in WIKIS:
        if config.FAKE_MODE:
            hits = [{"url": f"https://upload.wikimedia.org/fake/t-{urllib.parse.quote(title)}.jpg", "title": title,
                     "page": ""}]
        else:
            try:
                data = _get_json(api, {"action": "query", "format": "json", "formatversion": 2, "titles": title,
                                       "redirects": 1, "prop": "pageimages|info", "inprop": "url",
                                       "piprop": "thumbnail", "pithumbsize": 800, "pilicense": "free"})
            except (urllib.error.URLError, TimeoutError, ValueError):
                return None
            hits = parse_wiki_all(data)
        if hits and not bad_image(hits[0]["url"], hits[0]["title"]):
            pid = download(hits[0]["url"], "Wikipedia", hits[0]["title"], hits[0]["page"])
            if pid:
                break
    store.put("photo_q", key, {"id": pid})
    return pid


def destination_photo(landmark: str, name: str, region: str = "") -> str | None:
    """Ikonisches Bild eines Reiseziels: Wahrzeichen/Landschaft (Wikipedia-Titel der KI) → Suche danach →
    erst zuletzt der Ortsname (dessen Artikel zeigt oft nur ein Siegel oder eine Karte)."""
    landmark, name = (landmark or "").strip(), (name or "").split("(")[0].strip()
    if landmark:
        pid = wiki_title_photo(landmark) or wiki_photo(landmark, must_match=landmark)
        if pid:
            return pid
    return wiki_photo(f"{name} {region}".strip(), must_match=name)


def activity_photo(title: str, wiki: str | None, base: str) -> str | None:
    """Foto zu einem Programmpunkt: Wikipedia-Titel der KI → Suche «Titel Ort» → Suche «Titel»."""
    if wiki:
        pid = wiki_title_photo(wiki)
        if pid:
            return pid
    if GENERIC.match(title.strip()) or not _words(title):
        return None
    return wiki_photo(f"{title} {base}", must_match=title) or wiki_photo(title, must_match=title)


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
                    m["activities"][k] = activity_photo(a.title, a.wiki, b.name)
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
