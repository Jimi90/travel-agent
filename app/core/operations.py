"""Wendet Plan-Operationen auf eine Kopie des Reiseplans an (reine Funktion, ohne Speichern)."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta

from pydantic import ValidationError

from ..models import (
    AddActivity, AddStay, Activity, DayPlan, ExtendStay, Itinerary, MoveActivity, RemoveActivity, ReplaceActivity,
    SetConfirmed, SetFixed, SetTripStatus, ShiftSchedule,
)
from ..models.itinerary import Booking
from ..models.changes import PlanOperation


class PlanOperationError(ValueError):
    pass


def apply_operations(itin: Itinerary, ops: Iterable[PlanOperation]) -> Itinerary:
    """Gibt einen neuen, vollständig neu geprüften Plan zurück; das Original bleibt unverändert."""
    work = itin.model_copy(deep=True)
    for op in ops:
        _apply_one(work, op)
    try:
        return Itinerary.model_validate(work.model_dump())  # sortiert und prüft alles erneut
    except ValidationError as e:
        raise PlanOperationError(f"Plan wäre ungültig: {e.errors()[0]['msg']}") from e


def _take(itin: Itinerary, activity_id: str) -> tuple[int, Activity, list[Activity]]:
    found = itin.find_activity(activity_id)
    if not found:
        raise PlanOperationError(f"Aktivität {activity_id} nicht gefunden")
    day, act = found
    return day.activities.index(act), act, day.activities


def _apply_one(itin: Itinerary, op: PlanOperation) -> None:
    if isinstance(op, ReplaceActivity):
        idx, _, acts = _take(itin, op.activity_id)
        acts[idx] = op.new
    elif isinstance(op, MoveActivity):
        idx, act, acts = _take(itin, op.activity_id)
        target = itin.day(op.to_day)
        if target is None:
            raise PlanOperationError(f"Tag {op.to_day} gibt es im Plan nicht")
        acts.pop(idx)
        try:
            moved = Activity.model_validate({**act.model_dump(), "start": op.start, "end": op.end})
        except ValidationError as e:
            raise PlanOperationError(f"Verschieben ungültig: {e.errors()[0]['msg']}") from e
        target.activities.append(moved)
    elif isinstance(op, AddActivity):
        target = itin.day(op.day)
        if target is None:
            raise PlanOperationError(f"Tag {op.day} gibt es im Plan nicht")
        if itin.find_activity(op.activity.id):
            raise PlanOperationError(f"Aktivität {op.activity.id} gibt es schon")
        target.activities.append(op.activity)
    elif isinstance(op, RemoveActivity):
        idx, _, acts = _take(itin, op.activity_id)
        acts.pop(idx)
    elif isinstance(op, ExtendStay):
        stay = next((x for x in itin.stays if x.id == op.stay_id), None)
        if stay is None:
            raise PlanOperationError(f"Unterkunft {op.stay_id} nicht gefunden")
        if op.new_check_out <= stay.check_in:
            raise PlanOperationError("Abreise muss nach der Anreise liegen")
        nights_old = (stay.check_out - stay.check_in).days
        nights_new = (op.new_check_out - stay.check_in).days
        if stay.cost and nights_old:
            stay.cost = round(stay.cost / nights_old * nights_new, 2)
        stay.check_out = op.new_check_out
        if op.new_check_out - timedelta(days=1) > itin.end_date:
            itin.end_date = op.new_check_out - timedelta(days=1)
    elif isinstance(op, AddStay):
        if any(x.id == op.stay.id for x in itin.stays):
            raise PlanOperationError("Unterkunft gibt es schon")
        itin.stays.append(op.stay)
    elif isinstance(op, ShiftSchedule):
        _shift(itin, op)
    elif isinstance(op, SetTripStatus):
        itin.status = op.status
    elif isinstance(op, SetFixed):
        idx, act, acts = _take(itin, op.activity_id)
        acts[idx] = act.model_copy(update={"fixed": op.fixed})
    elif isinstance(op, SetConfirmed):
        items = itin.stays if op.target == "unterkunft" else itin.transports
        item = next((x for x in items if x.id == op.item_id), None)
        if item is None:
            raise PlanOperationError("Eintrag nicht gefunden")
        if op.target == "unterkunft":
            item.confirmed = op.confirmed
        else:
            item.estimated = not op.confirmed
        if op.confirmed and op.reference:
            item.booking = Booking(provider="eigene Buchung", reference=op.reference[:60])
        elif not op.confirmed:
            item.booking = None
    else:  # pragma: no cover
        raise PlanOperationError(f"unbekannte Operation {op!r}")


def _shift(itin: Itinerary, op: ShiftSchedule) -> None:
    """Ab ``from_date`` alles um ``days`` nach hinten; Lücke = leere Tage am Ort ``gap_base``."""
    delta = timedelta(days=op.days)
    if not itin.start_date <= op.from_date <= itin.end_date:
        raise PlanOperationError("Verschiebe-Datum liegt ausserhalb der Reise")
    for d in itin.days:
        if d.date >= op.from_date:
            d.date += delta
    for i in range(op.days):
        itin.days.append(DayPlan(date=op.from_date + timedelta(days=i), base=op.gap_base))
    for s in itin.stays:
        if s.check_in >= op.from_date:
            s.check_in += delta
            s.check_out += delta
        elif s.check_out >= op.from_date:  # Unterkunft am Ort, wo man festsitzt: verlängern
            nights_old = (s.check_out - s.check_in).days
            s.check_out += delta
            if s.cost and nights_old:
                s.cost = round(s.cost / nights_old * (nights_old + op.days), 2)
    for t in itin.transports:
        if t.depart.date() >= op.from_date:
            t.depart += delta
            t.arrive += delta
    itin.end_date += delta
