"""StatusManager: einzige Stelle, die Reisepläne verändert – atomar (ACID) dank SQLite.

Ablauf von ``apply`` in EINER Datenbank-Transaktion (``BEGIN IMMEDIATE``):
  1. Störung schon verarbeitet? → «doppelt» (event_id ist in der DB eindeutig)
  2. Version passt nicht mehr zum ChangeSet? → «konflikt» (optimistische Sperre)
  3. Alle Operationen auf eine Kopie anwenden → ungültig? → «abgelehnt»
  4. Plausibilitätsprüfung (MiddlewareConfig) → Fehler? → «abgelehnt»
  5. Neuer Plan + Protokolleintrag + Outbox-Aufträge schreiben, dann COMMIT
Fällt irgendetwas dazwischen aus (auch ein Absturz), bleibt der alte Plan vollständig erhalten.

Externe Buchungen lassen sich nicht in dieselbe Transaktion holen. Darum Outbox: Was bei
Booking.com & Co. passieren soll, wird mit der Planänderung zusammen gespeichert und danach
(nach Bestätigung) ausgeführt.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from ..models import ChangeRecord, ChangeResult, ChangeSet, ChangeStatus, Itinerary, Problem, TravelConstraints
from ..models.common import new_id
from .operations import PlanOperationError, apply_operations
from .plausibility import PlausibilityChecker

SCHEMA = """
CREATE TABLE IF NOT EXISTS itineraries (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    data TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS changes (
    id TEXT PRIMARY KEY,
    itinerary_id TEXT NOT NULL REFERENCES itineraries(id),
    from_version INTEGER NOT NULL,
    to_version INTEGER NOT NULL,
    event_id TEXT UNIQUE,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    operations TEXT NOT NULL,
    before TEXT NOT NULL,
    created_at TEXT NOT NULL,
    undone_by TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    change_id TEXT NOT NULL REFERENCES changes(id),
    action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'offen',
    created_at TEXT NOT NULL
);
"""


class StatusManager:
    def __init__(self, db_path: str | Path, checker: PlausibilityChecker | None = None):
        self.checker = checker or PlausibilityChecker()
        self._lock = threading.Lock()
        # isolation_level=None: Transaktionen steuern wir selbst mit BEGIN/COMMIT/ROLLBACK.
        self._db = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA foreign_keys = ON")
        if str(db_path) != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
        self._db.executescript(SCHEMA)

    # --- Lesen --------------------------------------------------------------------------------

    def get(self, itinerary_id: str) -> Itinerary:
        row = self._db.execute("SELECT data FROM itineraries WHERE id = ?", (itinerary_id,)).fetchone()
        if not row:
            raise KeyError(itinerary_id)
        return Itinerary.model_validate_json(row[0])

    def event_handled(self, event_id: str) -> bool:
        return self._db.execute("SELECT 1 FROM changes WHERE event_id = ?", (event_id,)).fetchone() is not None

    def history(self, itinerary_id: str) -> list[ChangeRecord]:
        rows = self._db.execute(
            "SELECT id, itinerary_id, from_version, to_version, event_id, actor, reason, operations,"
            " created_at, undone_by FROM changes WHERE itinerary_id = ? ORDER BY to_version",
            (itinerary_id,),
        ).fetchall()
        return [
            ChangeRecord(id=r[0], itinerary_id=r[1], from_version=r[2], to_version=r[3], event_id=r[4],
                         actor=r[5], reason=r[6], operations=json.loads(r[7]),
                         created_at=datetime.fromisoformat(r[8]), undone_by=r[9])
            for r in rows
        ]

    def pending_actions(self) -> list[dict]:
        rows = self._db.execute("SELECT id, change_id, action FROM outbox WHERE status = 'offen' ORDER BY created_at").fetchall()
        return [{"id": r[0], "change_id": r[1], **json.loads(r[2])} for r in rows]

    # --- Schreiben ----------------------------------------------------------------------------

    def create(self, itin: Itinerary) -> Itinerary:
        itin = Itinerary.model_validate({**itin.model_dump(), "version": 1})
        with self._lock:
            self._db.execute(
                "INSERT INTO itineraries (id, version, data, updated_at) VALUES (?, 1, ?, ?)",
                (itin.id, itin.model_dump_json(), datetime.now().isoformat()),
            )
        return itin

    def apply(self, cs: ChangeSet, constraints: TravelConstraints | None = None,
              now: datetime | None = None) -> ChangeResult:
        now = now or datetime.now()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                result = self._apply_in_tx(cs, constraints, now)
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT" if result.status is ChangeStatus.APPLIED else "ROLLBACK")
            return result

    def undo(self, change_id: str, now: datetime | None = None) -> ChangeResult:
        """Macht eine Änderung rückgängig – nur wenn danach nichts anderes geändert wurde."""
        now = now or datetime.now()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                result = self._undo_in_tx(change_id, now)
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT" if result.status is ChangeStatus.APPLIED else "ROLLBACK")
            return result

    # --- intern (läuft immer innerhalb einer offenen Transaktion) --------------------------------

    def _apply_in_tx(self, cs: ChangeSet, constraints: TravelConstraints | None, now: datetime) -> ChangeResult:
        res = lambda status, **kw: ChangeResult(status=status, itinerary_id=cs.itinerary_id, **kw)  # noqa: E731
        if cs.event_id and self.event_handled(cs.event_id):
            return res(ChangeStatus.DUPLICATE)
        row = self._db.execute("SELECT version, data FROM itineraries WHERE id = ?", (cs.itinerary_id,)).fetchone()
        if not row:
            return res(ChangeStatus.REJECTED, problems=[Problem(code="unbekannt", message="Reiseplan nicht gefunden")])
        version, data = row
        if version != cs.base_version:
            return res(ChangeStatus.CONFLICT, old_version=version, problems=[Problem(
                code="konflikt", message=f"Plan ist inzwischen Version {version}, erwartet {cs.base_version}")])
        before = Itinerary.model_validate_json(data)
        try:
            after = apply_operations(before, cs.operations)
        except PlanOperationError as e:
            return res(ChangeStatus.REJECTED, old_version=version, problems=[Problem(code="ungueltig", message=str(e))])
        problems = self.checker.check(before, after, constraints, now)
        if any(p.level == "fehler" for p in problems):
            return res(ChangeStatus.REJECTED, old_version=version, problems=problems)

        new_version = version + 1
        after = Itinerary.model_validate({**after.model_dump(), "version": new_version, "updated_at": now})
        cur = self._db.execute(
            "UPDATE itineraries SET version = ?, data = ?, updated_at = ? WHERE id = ? AND version = ?",
            (new_version, after.model_dump_json(), now.isoformat(), cs.itinerary_id, version),
        )
        if cur.rowcount != 1:  # sollte dank BEGIN IMMEDIATE nie passieren
            return res(ChangeStatus.CONFLICT, old_version=version)
        change_id = new_id()
        self._write_change(change_id, cs, version, new_version, data, now)
        self._write_outbox(change_id, cs, now)
        return res(ChangeStatus.APPLIED, old_version=version, new_version=new_version,
                   change_id=change_id, problems=problems)

    def _write_change(self, change_id: str, cs: ChangeSet, v_from: int, v_to: int, before: str, now: datetime) -> None:
        ops = json.dumps([op.model_dump(mode="json") for op in cs.operations], ensure_ascii=False)
        self._db.execute(
            "INSERT INTO changes (id, itinerary_id, from_version, to_version, event_id, actor, reason,"
            " operations, before, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (change_id, cs.itinerary_id, v_from, v_to, cs.event_id, cs.actor, cs.reason, ops, before, now.isoformat()),
        )

    def _write_outbox(self, change_id: str, cs: ChangeSet, now: datetime) -> None:
        for a in cs.external_actions:
            self._db.execute(
                "INSERT INTO outbox (id, change_id, action, created_at) VALUES (?, ?, ?, ?)",
                (a.id, change_id, a.model_dump_json(), now.isoformat()),
            )

    def _undo_in_tx(self, change_id: str, now: datetime) -> ChangeResult:
        row = self._db.execute(
            "SELECT itinerary_id, to_version, before, undone_by, reason FROM changes WHERE id = ?", (change_id,)
        ).fetchone()
        if not row:
            raise KeyError(change_id)
        itin_id, to_version, before, undone_by, reason = row
        res = lambda status, **kw: ChangeResult(status=status, itinerary_id=itin_id, **kw)  # noqa: E731
        if undone_by:
            return res(ChangeStatus.DUPLICATE)
        (current,) = self._db.execute("SELECT version FROM itineraries WHERE id = ?", (itin_id,)).fetchone()
        if current != to_version:
            return res(ChangeStatus.CONFLICT, old_version=current, problems=[Problem(
                code="konflikt", message="Nach dieser Änderung wurde der Plan weiter verändert")])
        new_version = current + 1
        restored = Itinerary.model_validate({**json.loads(before), "version": new_version, "updated_at": now})
        cur_data = self._db.execute("SELECT data FROM itineraries WHERE id = ?", (itin_id,)).fetchone()[0]
        self._db.execute("UPDATE itineraries SET version = ?, data = ?, updated_at = ? WHERE id = ?",
                         (new_version, restored.model_dump_json(), now.isoformat(), itin_id))
        undo_id = new_id()
        self._db.execute(
            "INSERT INTO changes (id, itinerary_id, from_version, to_version, event_id, actor, reason,"
            " operations, before, created_at) VALUES (?, ?, ?, ?, NULL, 'nutzer', ?, '[]', ?, ?)",
            (undo_id, itin_id, current, new_version, f"Rückgängig: {reason}", cur_data, now.isoformat()),
        )
        self._db.execute("UPDATE changes SET undone_by = ? WHERE id = ?", (undo_id, change_id))
        self._db.execute("UPDATE outbox SET status = 'verworfen' WHERE change_id = ? AND status = 'offen'", (change_id,))
        return res(ChangeStatus.APPLIED, old_version=current, new_version=new_version, change_id=undo_id)
