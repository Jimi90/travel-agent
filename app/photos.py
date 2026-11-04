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
from datetime import date

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


def wiki_photo(query: str, must_match: str = "", strict: bool = False) -> str | None:
    """Foto zum Suchbegriff; must_match: Wörter, von denen der Seitentitel mindestens eins enthalten muss.
    strict: bei mehreren Wörtern müssen zwei passen, und der Treffer mit den meisten gemeinsamen Wörtern gewinnt
    (sonst gewinnt bei «Playa Delfines» irgendein «Playa …»)."""
    key = ("wiki3:" if strict else "wiki2:") + _fold(query).strip()
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
        ok = [h for h in hits if (not need or need & _words(h["title"])) and not bad_image(h["url"], h["title"])]
        if strict and len(need) >= 2:
            ok = [h for h in ok if len(need & _words(h["title"])) >= 2]
            ok.sort(key=lambda h: -len(need & _words(h["title"])))
        hit = ok[0] if ok else None
        if hit:
            pid = download(hit["url"], "Wikipedia", hit["title"], hit["page"])
            if pid:
                break
    store.put("photo_q", key, {"id": pid})
    return pid


def wiki_title_photo(title: str) -> str | None:
    """Foto eines bestimmten Wikipedia-Artikels (Titel von der KI), deutsch zuerst, Weiterleitungen folgen."""
    key = "title2:" + _fold(title).strip()
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


def destination_photo(landmark: str, name: str, region: str = "", highlights=()) -> str | None:
    """Ikonisches Bild eines Reiseziels: Wahrzeichen/Landschaft (Wikipedia-Titel der KI) → Suche danach →
    erst zuletzt der Ortsname (dessen Artikel zeigt oft nur ein Siegel oder eine Karte)."""
    landmark, name = (landmark or "").strip(), (name or "").split("(")[0].strip()
    if landmark:
        pid = wiki_title_photo(landmark) or wiki_photo(landmark, must_match=landmark)
        if pid:
            return pid
    for h in list(highlights or [])[:3]:  # Highlights («Vulkan Poás») sind meist echte Wahrzeichen
        h = (h or "").strip()
        if len(h) >= 5:
            pid = wiki_photo(f"{h} {name}".strip(), must_match=h)
            if pid:
                return pid
    # Commons: freie Ortsfotos; Ortsname + Region/Wahrzeichen, auch bei kurzen Namen («Sal», «Rom»)
    for q, need in ((f"{landmark} {name}", landmark or name), (f"{name} {region}", name), (name, name)):
        if q.strip():
            pid = commons_photo(q.strip(), must_match=need)
            if pid:
                return pid
    pid = None
    if len(name) >= 5:  # kurzer Ortsname trifft in Wikipedia zu oft andere Artikel (Baum, Siegel)
        pid = wiki_photo(f"{name} {region}".strip(), must_match=name)
    # letzte Reserve: Google-Bildersuche (gemerkt, begrenzt pro Monat)
    return pid or google_photo(f"{landmark or name} {name} {region}".strip(), must_match=name)


COMMONS = "https://commons.wikimedia.org/w/api.php"


def parse_commons(data: dict) -> list[dict]:
    """Commons-Suche (Dateien, formatversion=2) → Querformat-Fotos (JPEG), beste Treffer zuerst."""
    pages = sorted((data.get("query") or {}).get("pages") or [], key=lambda p: p.get("index", 99))
    out = []
    for p in pages:
        ii = (p.get("imageinfo") or [{}])[0]
        url = ii.get("thumburl") or ""
        if not url or ii.get("mime") != "image/jpeg":
            continue
        w, h = ii.get("thumbwidth") or 0, ii.get("thumbheight") or 0
        if w and h and w < h:  # Hochformat passt schlecht in die Karte
            continue
        out.append({"url": url, "title": re.sub(r"^(File|Datei):", "", p.get("title", "")),
                    "page": ii.get("descriptionurl", "")})
    return out


def commons_photo(query: str, must_match: str = "") -> str | None:
    """Foto aus Wikimedia Commons (Millionen freier Ortsfotos). Dateititel muss ein Wort von must_match enthalten."""
    key = "commons1:" + _fold(query).strip() + "|" + _fold(must_match).strip()
    cached = store.get("photo_q", key)
    if cached is not None:
        return cached.get("id")
    need = _words(must_match)
    if config.FAKE_MODE:
        hits = [{"url": f"https://upload.wikimedia.org/fake/c-{urllib.parse.quote(query)}.jpg", "title": query,
                 "page": ""}]
    else:
        try:
            data = _get_json(COMMONS, {"action": "query", "format": "json", "formatversion": 2,
                                       "generator": "search", "gsrsearch": query, "gsrnamespace": 6, "gsrlimit": 10,
                                       "prop": "imageinfo", "iiprop": "url|mime|size", "iiurlwidth": 800})
        except (urllib.error.URLError, TimeoutError, ValueError):
            return None
        hits = parse_commons(data)
    pid = None
    for h in hits:
        if (need and not need & _words(h["title"])) or bad_image(h["url"], h["title"]):
            continue
        pid = download(h["url"], "Wikimedia Commons", h["title"], h["page"])
        if pid:
            break
    store.put("photo_q", key, {"id": pid})
    return pid


IMAGE_MONTH_CAP = 100  # Google-Bildersuchen pro Monat (Gratis-Kontingent SerpApi: 250, der Rest bleibt für Preise)


def image_usage() -> int:
    since = date.today().replace(day=1).isoformat()
    (n,) = store.db().execute("SELECT COUNT(*) FROM costs WHERE purpose = 'preise_bild' AND model = 'serpapi' "
                              "AND substr(ts, 1, 10) >= ?", (since,)).fetchone()
    return int(n)


def google_photo(query: str, must_match: str = "", trip_id: str | None = None) -> str | None:
    """Letzte Reserve: erstes passendes Bild der Google-Bildersuche (SerpApi). Das Ergebnis wird dauerhaft gemerkt
    (auch «nichts»), jede echte Suche zählt als Kosten-Zeile. Nur Vorschaubilder von Google (gstatic)."""
    from . import prices  # spät: prices braucht diese Datei nicht
    key = "gimg1:" + _fold(query).strip() + "|" + _fold(must_match).strip()
    cached = store.get("photo_q", key)
    if cached is not None:
        return cached.get("id")
    if not prices.available() or image_usage() >= IMAGE_MONTH_CAP:
        return None
    try:
        if config.FAKE_MODE:
            data = {"images_results": [{"title": query, "thumbnail": f"https://encrypted-tbn0.gstatic.com/fake-{_fold(query)}"}]}
            store.add_cost("preise_bild", "serpapi", 0.0, 0, 0, trip_id)
        else:
            data = prices._search({"engine": "google_images", "q": query, "hl": "de", "gl": "ch", "safe": "active"},
                                  "preise_bild", trip_id)
    except prices.PriceError:
        return None  # nichts merken, später nochmals
    need = _words(must_match)
    pid = None
    for r in (data.get("images_results") or [])[:8]:
        url, title = r.get("thumbnail") or "", str(r.get("title") or "")
        if (need and not need & _words(title)) or bad_image(url, title):
            continue
        pid = download(url, "Google Bilder", title, r.get("link") or "")
        if pid:
            break
    store.put("photo_q", key, {"id": pid})
    return pid


def place_photo(name: str, region: str = "") -> str | None:
    """Foto eines Ortes: Commons-Suche zuerst (zeigt Landschaft/Stadt), dann der Wikipedia-Artikel."""
    name = (name or "").split("(")[0].strip()
    if not name:
        return None
    pid = commons_photo(f"{name} {region}".strip(), must_match=name)
    if not pid and region:
        pid = commons_photo(name, must_match=name)
    return pid or wiki_photo(name, must_match=name) or google_photo(f"{name} {region} Sehenswürdigkeiten".strip(),
                                                                  must_match=name)


def has_photo_subject(a) -> bool:
    """Essen bekommt kein Foto (ein Teller Tacos sagt nichts über den Programmpunkt)."""
    cat = getattr(a.category, "value", a.category)
    return cat != "kulinarik"


def activity_photo(title: str, wiki: str | None, base: str) -> str | None:
    """Foto zu einem Programmpunkt: Wikipedia-Titel der KI → Suche «Titel Ort» → Suche «Titel»."""
    if wiki:
        pid = wiki_title_photo(wiki)
        if pid:
            return pid
    if GENERIC.match(title.strip()) or not _words(title):
        return None
    return (wiki_photo(f"{title} {base}", must_match=title, strict=True) or wiki_photo(title, must_match=title, strict=True)
            or commons_photo(f"{title} {base}", must_match=title))  # Commons kennt auch Strände, Riffe, Nationalparks


# --- Fotos einer Reise -------------------------------------------------------------------------

PHOTO_VERSION = 3  # 3: Commons-Suche dazu → leere Zuordnungen neu versuchen; 2: Wappen/Logos aussortiert → ältere Zuordnungen (Orte, Programmpunkte) werden neu geholt


def trip_photos(trip_id: str) -> dict:
    m = store.get("trip_photos", trip_id) or {"places": {}, "activities": {}, "stays": {}}
    if m.get("v") != PHOTO_VERSION:
        m = {"v": PHOTO_VERSION, "places": {}, "activities": {}, "stays": m.get("stays") or {}}
    return m


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
                    m["places"][b.name] = place_photo(b.name)
                    store.put("trip_photos", itin.id, m)
                for a in d.activities:
                    k = activity_key(a.title)
                    if k in m["activities"]:
                        continue
                    m["activities"][k] = activity_photo(a.title, a.wiki, b.name) if has_photo_subject(a) else None
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
