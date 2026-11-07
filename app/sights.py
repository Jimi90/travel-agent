"""Sehenswürdigkeiten und Ausflüge im Tagesplan: TripAdvisor-Bewertung, Link und ein Tipp aus den Bewertungen
(Wunsch James 1.15.0: «Berücksichtigen wir Tipps von TripAdvisor?»).

Wie die Restaurant-Bewertungen (`venues.py`), aber über SerpApi ``engine=tripadvisor`` (seit August 2025): eine Suche
je Programmpunkt im Hintergrund, dauerhaft gemerkt (kv «sight_live», auch «nicht gefunden»), höchstens
``MAX_SEARCHES_PER_RUN`` pro Lauf und ``MONTH_CAP`` im Monat (Gratis-Kontingent 250 teilen sich Flüge, Hotels,
Restaurants, Bilder). Die hervorgehobene Bewertung macht eine kleine KI-Aufgabe zu einem deutschen Tipp (ein Aufruf
für alle neuen Punkte, Groq/Gemini gratis). Klar schwach bewertete Punkte ersetzt Kompass im Entwurf selbst durch eine
der am besten bewerteten Sehenswürdigkeiten am Ort (eine Änderung, rückgängig machbar).
Feldnamen nach SerpApi-Doku (serpapi.com aus der Cloud gesperrt) – echte Antworten nur auf dem NAS prüfbar.
"""
from __future__ import annotations

import re
import threading
import urllib.parse
from datetime import date, datetime, timedelta

from pydantic import BaseModel

from . import config, llm, prices, store
from .hotels import same_hotel
from .photos import GENERIC

MAX_SEARCHES_PER_RUN = 10
MONTH_CAP = 60
RETRY_ERROR_H = 6
PURPOSE = "preise_tripadvisor"
WEAK_RATING = 4.0  # TripAdvisor bewertet Sehenswürdigkeiten grosszügig – unter 4.0 ist wirklich schwach
WEAK_MIN_REVIEWS = 50  # erst mit genug Bewertungen aussagekräftig
GOOD_RATING = 4.5  # Ersatz nur mit ≥ 4.5 …
GOOD_REVIEWS = 200  # … und vielen Bewertungen
CATS = {"wandern", "natur", "strand", "wassersport", "kultur", "museum", "stadt", "wellness", "shopping", "nachtleben"}

_lock = threading.Lock()
_running: set[str] = set()


def _cat(a) -> str:
    return str(getattr(a.category, "value", a.category))


def subject(a) -> str | None:
    """Wonach Kompass bei TripAdvisor sucht: der Wikipedia-Titel, den die KI nur für echte Orte und Sehenswürdigkeiten
    nennt (nie für «Spaziergang», «Strandtag», Essen). Am Titel allein lässt sich das nicht erkennen – im Deutschen
    sind alle Nomen gross geschrieben – und jede Suche kostet vom Gratis-Kontingent."""
    if _cat(a) not in CATS:
        return None
    w = re.sub(r"\s*\(.*?\)\s*$", "", (getattr(a, "wiki", None) or "").replace("_", " ")).strip()
    return w[:120] if len(w) >= 3 and not GENERIC.match(w) else None


def key(name: str, place: str) -> str:
    return f"{name.strip().lower()}|{place.strip().lower()}"


def cached(name: str, place: str) -> dict | None:
    return store.get("sight_live", key(name, place))


def search_url(name: str, place: str) -> str:
    return "https://www.tripadvisor.com/Search?" + urllib.parse.urlencode({"q": f"{name} {place}".strip()})


def month_usage() -> int:
    since = date.today().replace(day=1).isoformat()
    (n,) = store.db().execute("SELECT COUNT(*) FROM costs WHERE purpose = ? AND model = 'serpapi' AND ts >= ?",
                              (PURPOSE, since)).fetchone()
    return int(n)


def _due(name: str, place: str) -> bool:
    c = cached(name, place)
    if c is None:
        return True
    return bool(c.get("error")) and datetime.now() - datetime.fromisoformat(c["ts"]) > timedelta(hours=RETRY_ERROR_H)


def _items(it):
    for d in it.days:
        for a in d.activities:
            s = subject(a)
            if s:
                yield a, s, (a.place.name if a.place else d.base.name)


def weak(c: dict | None) -> bool:
    """Nur wenn TripAdvisor den Ort kennt und er klar schlecht bewertet ist (nicht gefunden = kein Urteil)."""
    return bool(c and c.get("found") and c.get("rating") is not None and c["rating"] < WEAK_RATING
                and (c.get("reviews") or 0) >= WEAK_MIN_REVIEWS)


def good(c: dict) -> bool:
    return (c.get("rating") or 0) >= GOOD_RATING and (c.get("reviews") or 0) >= GOOD_REVIEWS


def alternatives(place: str) -> list[dict] | None:
    return store.get("sight_alts", place.strip().lower())


def _alts_due(it) -> list[str]:
    out = []
    for _, s, p in _items(it):
        if weak(cached(s, p)) and alternatives(p) is None and p not in out:
            out.append(p)
    return out


def _tips_due(it) -> bool:
    f = store.get("sight_tipfail", it.id)  # KI gerade nicht erreichbar → 6 Std. nicht nochmals fragen
    if f and datetime.now() - datetime.fromisoformat(f["ts"]) < timedelta(hours=RETRY_ERROR_H):
        return False
    return any((c := cached(s, p)) and c.get("found") and c.get("quote") and "tip" not in c for _, s, p in _items(it))


def missing(it) -> bool:
    if not prices.available():
        return False
    return (month_usage() < MONTH_CAP and (any(_due(s, p) for _, s, p in _items(it)) or bool(_alts_due(it)))) \
        or _tips_due(it)


# --- SerpApi-Antwort lesen ---------------------------------------------------------------------

def _num(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"\d+(?:[.,]\d+)?", v)
        return float(m.group(0).replace(",", ".")) if m else None
    return None


def _count(v) -> int | None:
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        digits = re.sub(r"[^\d]", "", v)
        return int(digits) if digits else None
    return None


def _quote(r: dict) -> str:
    h = r.get("highlighted_review")
    if isinstance(h, dict):
        h = h.get("text") or h.get("snippet")
    return re.sub(r"\s+", " ", str(h or "")).strip()[:400]


def _entry(r: dict) -> dict:
    link = str(r.get("link") or "")
    return {"found": True, "title": str(r.get("title"))[:160],
            "rating": _num(r.get("rating")), "reviews": _count(r.get("reviews")),
            "link": link if link.startswith("https://") and ".tripadvisor." in link.split("/")[2] else None,
            "description": re.sub(r"\s+", " ", str(r.get("description") or "")).strip()[:400],
            "quote": _quote(r), "location": str(r.get("location") or "")[:120]}


def _results(data: dict) -> list[dict]:
    for k in ("places", "results", "organic_results"):
        if isinstance(data.get(k), list):
            return [r for r in data[k] if isinstance(r, dict) and r.get("title")]
    return []


def pick(data: dict, name: str, place: str) -> dict | None:
    for r in _results(data):
        if same_hotel(name, str(r["title"]), place):
            return r
    return None


def _search(q: str, trip_id: str | None) -> dict:
    # ssrc=A: nur «Things to Do» (keine Hotels/Restaurants)
    return prices._search({"engine": "tripadvisor", "q": q, "ssrc": "A"}, PURPOSE, trip_id)


def fetch(name: str, place: str, trip_id: str | None) -> dict:
    result: dict = {"ts": datetime.now().isoformat(), "found": False}
    try:
        data = _fake(name, place) if config.FAKE_MODE else _search(f"{name} {place}", trip_id)
        r = pick(data, name, place)
        if r:
            result.update(_entry(r))
    except prices.PriceError as e:
        result["error"] = str(e)
    store.put("sight_live", key(name, place), result)
    return result


def fetch_alternatives(place: str, trip_id: str | None) -> list[dict]:
    """Die am besten bewerteten Sehenswürdigkeiten am Ort (eine Suche je Ort, dauerhaft gemerkt)."""
    try:
        data = _fake_alts(place) if config.FAKE_MODE else _search(f"Sehenswürdigkeiten {place}", trip_id)
    except prices.PriceError as e:
        print(f"[TripAdvisor] Alternativen {place}: {e}", flush=True)
        return []  # nicht merken – später nochmals
    out = [e for e in (_entry(r) for r in _results(data)) if good(e)]
    out.sort(key=lambda c: (-(c["rating"] or 0), -(c["reviews"] or 0)))
    store.put("sight_alts", place.strip().lower(), out[:8])
    return out[:8]


# --- Tipps (kleine KI-Aufgabe) -----------------------------------------------------------------

class Tip(BaseModel):
    n: int
    tip: str = ""


class TipList(BaseModel):
    tips: list[Tip]


TIP_SYSTEM = ("Du bist Kompass, ein Reiseplaner. Deutsch, Schweizer Rechtschreibung (kein ß), «ihr/euch», nie «Sie». "
              "Du machst aus Auszügen echter Besucher-Bewertungen je EINEN praktischen Tipp (höchstens 16 Wörter): "
              "beste Zeit, Tickets, Schlangen, was man nicht verpassen soll, was man mitnehmen soll. Nichts erfinden, "
              "keine Bewertung wiederholen («toll», «schön»). Steckt kein praktischer Tipp drin: tip leer lassen.")


def tips_for(todo: list[tuple[str, str, dict]], trip_id: str | None = None) -> bool:
    """[(Suchname, Ort, Eintrag)] mit Bewertungsauszug → in einem KI-Aufruf deutsche Tipps, im Eintrag unter «tip»
    gemerkt. False, wenn die KI nicht erreichbar war (dann bleibt alles ohne Tipp)."""
    todo = todo[:20]
    if not todo:
        return True
    lines = "\n".join(f"{i}. {c.get('title') or s} ({p}): «{c['quote']}»" for i, (s, p, c) in enumerate(todo, 1))
    try:
        res = llm.ask("tipps", system=TIP_SYSTEM, trip_id=trip_id, max_tokens=2000, schema=TipList,
                      content=f"Bewertungsauszüge:\n{lines}\n\nGib für jede Nummer n einen Tipp zurück.",
                      fake=lambda: TipList(tips=[Tip(n=i, tip="Früh am Morgen hingehen, ab 11 Uhr wird es voll.")
                                                 for i in range(1, len(todo) + 1)]))
    except llm.LLMError as e:
        print(f"[TripAdvisor] Tipps: {e}", flush=True)
        return False
    got = {t.n: re.sub(r"\s+", " ", t.tip).strip().strip("«»\"")[:160] for t in res.tips}
    for i, (s, p, c) in enumerate(todo, 1):
        c["tip"] = got.get(i, "")
        store.put("sight_live", key(s, p), c)
    return True


def make_tips(it) -> int:
    """Alle Bewertungsauszüge ohne Tipp in einem KI-Aufruf zu deutschen Tipps machen. Gibt die Anzahl zurück."""
    todo = []
    for _, s, p in _items(it):
        c = cached(s, p)
        if c and c.get("found") and c.get("quote") and "tip" not in c and (s, p) not in [(x[0], x[1]) for x in todo]:
            todo.append((s, p, c))
    if not todo or not _tips_due(it):
        return 0
    if not tips_for(todo, it.id):
        store.put("sight_tipfail", it.id, {"ts": datetime.now().isoformat()})
        return 0  # kein Tipp gemerkt – in 6 Std. nochmals
    return len(todo[:20])


def lookup(name: str, place: str, trip_id: str | None = None) -> dict | None:
    """Eine Sehenswürdigkeit bei TripAdvisor nachschlagen (gemerkt oder neu, im Monatslimit) – für Ausflugsideen."""
    if not prices.available():
        return None
    if _due(name, place):
        if month_usage() >= MONTH_CAP:
            return None
        return fetch(name, place, trip_id)
    return cached(name, place)


def nearby(query: str, lat: float, lon: float, trip_id: str | None = None) -> list[dict]:
    """Beliebte Orte/Touren bei TripAdvisor rund um eine Position (Ideen für Tagesausflüge), 1 Suche, 7 Tage gemerkt."""
    k = f"{query.strip().lower()}|{lat:.2f},{lon:.2f}"
    hit = store.get("sight_nearby", k)
    if hit and datetime.now() - datetime.fromisoformat(hit["ts"]) < timedelta(days=7):
        return hit["items"]
    if not prices.available() or month_usage() >= MONTH_CAP:
        return []
    try:
        data = _fake_nearby() if config.FAKE_MODE else prices._search(
            {"engine": "tripadvisor", "q": query, "lat": f"{lat:.4f}", "lon": f"{lon:.4f}", "ssrc": "A"}, PURPOSE, trip_id)
    except prices.PriceError as e:
        print(f"[TripAdvisor] Umgebung: {e}", flush=True)
        return []
    items = [e for e in (_entry(r) for r in _results(data)) if (e.get("rating") or 0) >= 4.0][:12]
    store.put("sight_nearby", k, {"ts": datetime.now().isoformat(), "items": items})
    return items


# --- Hintergrund ---------------------------------------------------------------------------------

def enrich(it) -> None:
    """Im Hintergrund fehlende TripAdvisor-Bewertungen, Alternativen und Tipps holen."""
    with _lock:
        if it.id in _running:
            return
        _running.add(it.id)

    def run():
        n = 0
        try:
            seen = set()
            for _, s, p in _items(it):
                if n >= MAX_SEARCHES_PER_RUN or month_usage() >= MONTH_CAP:
                    break
                k = key(s, p)
                if k in seen or not _due(s, p):
                    continue
                seen.add(k)
                fetch(s, p, it.id)
                n += 1
            for p in _alts_due(it):
                if n >= MAX_SEARCHES_PER_RUN or month_usage() >= MONTH_CAP:
                    break
                fetch_alternatives(p, it.id)
                n += 1
            make_tips(it)
            auto_improve(it.id)
        except Exception as e:  # nie die App stören
            print(f"[TripAdvisor] {it.id}: {type(e).__name__}: {e}", flush=True)
        finally:
            with _lock:
                _running.discard(it.id)

    if config.FAKE_MODE and getattr(enrich, "sync", False):
        run()
    else:
        threading.Thread(target=run, daemon=True, name=f"tripadvisor-{it.id}").start()


def for_trip(it) -> dict:
    """Pro Programmpunkt mit Treffer: {name, url, rating, reviews, tip, weak}."""
    out = {}
    for a, s, p in _items(it):
        c = cached(s, p) or {}
        if not c.get("found"):
            continue
        out[a.id] = {"name": c.get("title") or s, "url": c.get("link") or search_url(c.get("title") or s, p),
                     "rating": c.get("rating"), "reviews": c.get("reviews"), "tip": c.get("tip") or "",
                     "weak": weak(c)}
    return out


# --- schwach bewertete Punkte selbst ersetzen ----------------------------------------------------

def improvable(it) -> dict:
    """{activity_id: (activity, besserer Ort, Ort)} – nur im Entwurf, nie festgenagelt oder gebucht, nie doppelt."""
    if getattr(it, "status", "entwurf") != "entwurf":
        return {}
    items = list(_items(it))
    used = [a.title for a, _, _ in items] + [(cached(n, p) or {}).get("title") or n for _, n, p in items]
    out = {}
    for a, s, p in items:
        if a.fixed or a.booking or not weak(cached(s, p)):
            continue
        for alt in alternatives(p) or []:
            if any(same_hotel(u, alt["title"], p) or u.casefold() == alt["title"].casefold() for u in used):
                continue
            used.append(alt["title"])
            out[a.id] = (a, alt, p)
            break
    return out


def swapped(a, better: dict):
    """Programmpunkt mit der besser bewerteten Sehenswürdigkeit (Zeit, Kategorie, Ort bleiben)."""
    cnt = f"{better['reviews']:,}".replace(",", "'") if better.get("reviews") else ""
    note = (better.get("description") or "").strip()
    note = (note + " " if note else "") + (f"Bei TripAdvisor sehr gut bewertet: ★ {better['rating']:.1f}"
                                           + (f" ({cnt} Bewertungen)." if cnt else "."))
    return a.model_copy(update={"title": better["title"][:160], "note": note[:600], "wiki": better["title"][:200],
                                "venue": None})


def auto_improve(trip_id: str) -> int:
    from . import service  # spät laden (service → server → sights)
    from .models import ReplaceActivity
    it = service.sm().get(trip_id)
    todo = improvable(it)
    if not todo:
        return 0
    ops = []
    for aid, (a, b, place) in todo.items():
        new = swapped(a, b)
        ops.append(ReplaceActivity(activity_id=aid, new=new))
        store.put("sight_live", key(subject(new) or new.title, place), {"ts": datetime.now().isoformat(), **b})
    names = ", ".join(b["title"] for _, b, _ in todo.values())
    out = service.change(trip_id, ops, f"Besser bewertet bei TripAdvisor: {names}"[:300], actor="autonom")
    if out["status"] != "angewendet":
        print(f"[TripAdvisor] {trip_id}: Tausch nicht übernommen ({out['problems']})", flush=True)
        return 0
    return len(ops)


# --- Testmodus ------------------------------------------------------------------------------------

def _fake(name: str, place: str) -> dict:
    h = sum(map(ord, name))
    rating = 3.5 if "Museum 2" in name or "Museu 2" in name else round(4.0 + (h % 10) / 10, 1)
    return {"places": [{"title": name, "rating": rating, "reviews": 300 + h % 4000, "location": place,
                        "link": f"https://www.tripadvisor.com/Attraction_Review-test{h}",
                        "description": "Testbeschreibung.",
                        "highlighted_review": {"text": "Go early in the morning, the queue gets long after 11."}}]}


def _fake_nearby() -> dict:
    return {"places": [{"title": "Rigi Kulm", "rating": 4.8, "reviews": 3120, "location": "Rigi, Schweiz"},
                       {"title": "Rheinfall", "rating": 4.6, "reviews": 8800, "location": "Neuhausen, Schweiz"},
                       {"title": "Kapellbrücke", "rating": 4.5, "reviews": 12500, "location": "Luzern, Schweiz"}]}


def _fake_alts(place: str) -> dict:
    return {"places": [{"title": f"Castelo de {place}", "rating": 4.5, "reviews": 25118, "location": place,
                        "link": "https://www.tripadvisor.com/Attraction_Review-alt1",
                        "description": "Burg hoch über der Stadt mit Blick über die Dächer."},
                       {"title": "Kleiner Park", "rating": 4.0, "reviews": 90}]}
