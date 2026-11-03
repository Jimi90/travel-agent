"""Kartenkacheln von OpenStreetMap für die kleinen Routenkarten.

Der Server holt die Kacheln selbst (eigener Absender, höchstens 2 gleichzeitig, 30 Tage zwischengespeichert –
so verlangt es die Nutzungsrichtlinie von tile.openstreetmap.org) und liefert sie aus. Die Seite lädt nichts
von fremden Servern (CSP img-src 'self' bleibt). Quellenangabe «© OpenStreetMap» steht auf jeder Karte.
"""
from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request

from . import config

UA = f"Kompass/{config.APP_VERSION} (privater Reiseplaner, wenige Abrufe)"
URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
MAX_ZOOM = 16
MAX_AGE = 30 * 86400
_slots = threading.BoundedSemaphore(2)


def valid(z: int, x: int, y: int) -> bool:
    return 0 <= z <= MAX_ZOOM and 0 <= x < 2 ** z and 0 <= y < 2 ** z


def get(z: int, x: int, y: int) -> tuple[bytes, str] | None:
    if not valid(z, x, y):
        return None
    if config.FAKE_MODE:
        return _fake(z, x, y), "image/svg+xml"
    f = config.DATA_DIR / "tiles" / str(z) / str(x) / f"{y}.png"
    if f.exists() and time.time() - f.stat().st_mtime < MAX_AGE:
        return f.read_bytes(), "image/png"
    with _slots:
        req = urllib.request.Request(URL.format(z=z, x=x, y=y), headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                if (r.headers.get("Content-Type") or "").split(";")[0] != "image/png":
                    return None
                data = r.read(400_000)
        except (urllib.error.URLError, TimeoutError):
            return (f.read_bytes(), "image/png") if f.exists() else None  # alte Kachel besser als keine
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return data, "image/png"


def _fake(z: int, x: int, y: int) -> bytes:
    shade = 225 + (x + y) % 2 * 12
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><rect width="256" height="256" '
            f'fill="rgb({shade - 20},{shade},{shade - 10})"/><path d="M0 128H256M128 0V256" stroke="#fff" '
            f'stroke-width="3"/><text x="8" y="20" font-size="12" fill="#789">{z}/{x}/{y}</text></svg>').encode()
