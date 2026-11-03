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


class _Result:
    def __init__(self, rows: list, rowcount: int):
        self._rows, self.rowcount = rows, rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class StatusManager:
    def __init__(self, db_path: str | Path, checker: PlausibilityChecker | None = None):
        self.checker = checker or PlausibilityChecker()
        self._lock = threading.RLock()  # auch Lesen läuft darunter (eine Verbindung für alle Threads)
        # isolation_level=None: Transaktionen steuern wir selbst mit BEGIN/COMMIT/ROLLBACK.
        self._db = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
        self._ex("PRAGMA foreign_keys = ON")
        if str(db_path) != ":memory:":
            self._ex("PRAGMA journal_mode = WAL")
            self._ex("PRAGMA synchronous = FULL")
        self._db.executescript(SCHEMA)

    def _ex(self, sql: str, args=()):
        """Jeder Zugriff unter der Sperre; Ergebnis sofort vollständig gelesen."""
        with self._lock:
            cur = self._db.execute(sql, args)
            return _Result(cur.fetchall(), cur.rowcount)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- Lesen --------------------------------------------------------------------------------

    def get(self, itinerary_id: str) -> Itinerary:
        row = self._ex("SELECT data FROM itineraries WHERE id = ?", (itinerary_id,)).fetchone()
        if not row:
            raise KeyError(itinerary_id)
        return Itinerary.model_validate_json(row[0])

    def list(self) -> list[Itinerary]:
        rows = self._ex("SELECT data FROM itineraries ORDER BY updated_at DESC").fetchall()
        return [Itinerary.model_validate_json(r[0]) for r in rows]

    def actions(self, itinerary_id: str | None = None, status: str | None = "offen") -> list[dict]:
        q = ("SELECT o.id, o.change_id, o.action, o.status, o.created_at, c.itinerary_id FROM outbox o"
             " JOIN changes c ON c.id = o.change_id WHERE 1=1")
        args: list = []
        if itinerary_id:
            q += " AND c.itinerary_id = ?"
            args.append(itinerary_id)
        if status:
            q += " AND o.status = ?"
            args.append(status)
        rows = self._ex(q + " ORDER BY o.created_at", args).fetchall()
        return [{**json.loads(r[2]), "id": r[0], "change_id": r[1], "status": r[3], "created_at": r[4],
                 "itinerary_id": r[5]} for r in rows]

    def set_action_status(self, action_id: str, status: str) -> bool:
        if status not in ("offen", "erledigt", "verworfen"):
            raise ValueError(status)
        with self._lock:
            cur = self._ex("UPDATE outbox SET status = ? WHERE id = ?", (status, action_id))
            return cur.rowcount == 1

    def delete(self, itinerary_id: str) -> bool:
        with self._lock:
            self._ex("BEGIN IMMEDIATE")
            try:
                self._ex("DELETE FROM outbox WHERE change_id IN (SELECT id FROM changes WHERE itinerary_id = ?)", (itinerary_id,))
                self._ex("DELETE FROM changes WHERE itinerary_id = ?", (itinerary_id,))
                cur = self._ex("DELETE FROM itineraries WHERE id = ?", (itinerary_id,))
            except BaseException:
                self._ex("ROLLBACK")
                raise
            self._ex("COMMIT")
            return cur.rowcount == 1

    def event_handled(self, event_id: str) -> bool:
        return self._ex("SELECT 1 FROM changes WHERE event_id = ?", (event_id,)).fetchone() is not None

    def history(self, itinerary_id: str) -> list[ChangeRecord]:
        rows = self._ex(
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
        rows = self._ex("SELECT id, change_id, action FROM outbox WHERE status = 'offen' ORDER BY created_at").fetchall()
        return [{"id": r[0], "change_id": r[1], **json.loads(r[2])} for r in rows]

    # --- Schreiben ----------------------------------------------------------------------------

    def create(self, itin: Itinerary) -> Itinerary:
        itin = Itinerary.model_validate({**itin.model_dump(), "version": 1})
        with self._lock:
            self._ex(
                "INSERT INTO itineraries (id, version, data, updated_at) VALUES (?, 1, ?, ?)",
                (itin.id, itin.model_dump_json(), datetime.now().isoformat()),
            )
        return itin

    def apply(self, cs: ChangeSet, constraints: TravelConstraints | None = None,
              now: datetime | None = None) -> ChangeResult:
        now = now or datetime.now()
        with self._lock:
            self._ex("BEGIN IMMEDIATE")
            try:
                result = self._apply_in_tx(cs, constraints, now)
            except BaseException:
                self._ex("ROLLBACK")
                raise
            self._ex("COMMIT" if result.status is ChangeStatus.APPLIED else "ROLLBACK")
            return result

    def undo(self, change_id: str, now: datetime | None = None) -> ChangeResult:
        """Macht eine Änderung rückgängig – nur wenn danach nichts anderes geändert wurde."""
        now = now or datetime.now()
        with self._lock:
            self._ex("BEGIN IMMEDIATE")
            try:
                result = self._undo_in_tx(change_id, now)
            except BaseException:
                self._ex("ROLLBACK")
                raise
            self._ex("COMMIT" if result.status is ChangeStatus.APPLIED else "ROLLBACK")
            return result

    # --- intern (läuft immer innerhalb einer offenen Transaktion) --------------------------------

    def _apply_in_tx(self, cs: ChangeSet, constraints: TravelConstraints | None, now: datetime) -> ChangeResult:
        res = lambda status, **kw: ChangeResult(status=status, itinerary_id=cs.itinerary_id, **kw)  # noqa: E731
        if cs.event_id and self.event_handled(cs.event_id):
            return res(ChangeStatus.DUPLICATE)
        row = self._ex("SELECT version, data FROM itineraries WHERE id = ?", (cs.itinerary_id,)).fetchone()
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
        problems = self.checker.check(before, after, constraints, now, set(cs.acknowledged_locked))
        if any(p.level == "fehler" for p in problems):
            return res(ChangeStatus.REJECTED, old_version=version, problems=problems)

        new_version = version + 1
        after = Itinerary.model_validate({**after.model_dump(), "version": new_version, "updated_at": now})
        cur = self._ex(
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
        self._ex(
            "INSERT INTO changes (id, itinerary_id, from_version, to_version, event_id, actor, reason,"
            " operations, before, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (change_id, cs.itinerary_id, v_from, v_to, cs.event_id, cs.actor, cs.reason, ops, before, now.isoformat()),
        )

    def _write_outbox(self, change_id: str, cs: ChangeSet, now: datetime) -> None:
        for a in cs.external_actions:
            self._ex(
                "INSERT INTO outbox (id, change_id, action, created_at) VALUES (?, ?, ?, ?)",
                (a.id, change_id, a.model_dump_json(), now.isoformat()),
            )

    def _undo_in_tx(self, change_id: str, now: datetime) -> ChangeResult:
        row = self._ex(
            "SELECT itinerary_id, to_version, before, undone_by, reason FROM changes WHERE id = ?", (change_id,)
        ).fetchone()
        if not row:
            raise KeyError(change_id)
        itin_id, to_version, before, undone_by, reason = row
        res = lambda status, **kw: ChangeResult(status=status, itinerary_id=itin_id, **kw)  # noqa: E731
        if undone_by:
            return res(ChangeStatus.DUPLICATE)
        (current,) = self._ex("SELECT version FROM itineraries WHERE id = ?", (itin_id,)).fetchone()
        if current != to_version:
            return res(ChangeStatus.CONFLICT, old_version=current, problems=[Problem(
                code="konflikt", message="Nach dieser Änderung wurde der Plan weiter verändert")])
        new_version = current + 1
        restored = Itinerary.model_validate({**json.loads(before), "version": new_version, "updated_at": now})
        cur_data = self._ex("SELECT data FROM itineraries WHERE id = ?", (itin_id,)).fetchone()[0]
        self._ex("UPDATE itineraries SET version = ?, data = ?, updated_at = ? WHERE id = ?",
                         (new_version, restored.model_dump_json(), now.isoformat(), itin_id))
        undo_id = new_id()
        self._ex(
            "INSERT INTO changes (id, itinerary_id, from_version, to_version, event_id, actor, reason,"
            " operations, before, created_at) VALUES (?, ?, ?, ?, NULL, 'nutzer', ?, '[]', ?, ?)",
            (undo_id, itin_id, current, new_version, f"Rückgängig: {reason}", cur_data, now.isoformat()),
        )
        self._ex("UPDATE changes SET undone_by = ? WHERE id = ?", (undo_id, change_id))
        self._ex("UPDATE outbox SET status = 'verworfen' WHERE change_id = ? AND status = 'offen'", (change_id,))
        return res(ChangeStatus.APPLIED, old_version=current, new_version=new_version, change_id=undo_id)
