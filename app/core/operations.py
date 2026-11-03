"""Wendet Plan-Operationen auf eine Kopie des Reiseplans an (reine Funktion, ohne Speichern)."""
from __future__ import annotations

from collections.abc import Iterable

from pydantic import ValidationError

from ..models import AddActivity, Activity, Itinerary, MoveActivity, RemoveActivity, ReplaceActivity
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
    else:  # pragma: no cover
        raise PlanOperationError(f"unbekannte Operation {op!r}")
