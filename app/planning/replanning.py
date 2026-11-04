"""AutonomousRePlanningModule: stellt den Tagesplan bei Schlechtwetter selbständig um.

Für jede betroffene Aktivität (draussen, Wetter über ihren Grenzwerten) werden Optionen in
dieser Reihenfolge probiert; die erste, die die Plausibilitätsprüfung besteht, gewinnt:
  1. Verschieben auf einen trockenen Tag am selben Ort (+ freie Lücke mit Indoor-Ersatz füllen)
  2. Verschieben ohne Ersatz
  3. Ersetzen durch die beste wetterfeste Alternative
Fest gebuchte/festgenagelte Aktivitäten bleiben, James bekommt einen Hinweis.
Alle Schritte zusammen gehen als EIN ChangeSet an den StatusManager → ganz oder gar nicht.
Ändert sich der Plan in der Zwischenzeit (Konflikt), wird mit dem neuen Stand neu geplant.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal

from ..core.operations import PlanOperationError, apply_operations
from ..core.plausibility import MiddlewareConfig
from ..core.status_manager import StatusManager
from ..models import (
    Activity, AddActivity, ChangeResult, ChangeSet, ChangeStatus, ClosureDisruption, DayPlan, ExternalAction,
    Household, Itinerary, MoveActivity, Place, ReplaceActivity, WeatherDisruption, WeatherForecast,
)
from ..models.changes import PlanOperation
from ..models.common import Model, from_min, to_min
from .alternatives import AlternativeActivitiesModule

ForecastLookup = Callable[[Place, date], WeatherForecast | None]


class ReplanDecision(Model):
    activity_id: str
    title: str
    date: date
    action: Literal["verschoben", "verschoben_mit_ersatz", "ersetzt", "unveraendert"]
    reason: str
    detail: str = ""
    new_activity_id: str | None = None


class ReplanOutcome(Model):
    event_id: str
    status: ChangeStatus
    decisions: list[ReplanDecision] = []
    needs_user: list[str] = []
    result: ChangeResult | None = None
    attempts: int = 0

    def message(self) -> str:
        """Kurzer Text für die Push-Meldung am Morgen."""
        lines = [f"{d.date:%d.%m.} «{d.title}»: {d.detail}" for d in self.decisions if d.action != "unveraendert"]
        lines += self.needs_user
        return "\n".join(lines) or "Nichts betroffen – das Geplante passt zu diesem Wetter."


def find_free_slot(day: DayPlan, duration: int, preferred: time, cfg: MiddlewareConfig) -> tuple[time, time] | None:
    """Freies Zeitfenster gleicher Länge, möglichst nahe an der gewohnten Startzeit."""
    lo, hi, gap = to_min(cfg.day_start), to_min(cfg.day_end), cfg.min_gap_minutes
    busy = [(to_min(a.start) - gap, to_min(a.end) + gap) for a in day.activities]
    starts = sorted(range(lo, hi - duration + 1, 15), key=lambda s: (abs(s - to_min(preferred)), s))
    for s in starts:
        if all(s + duration <= b0 or s >= b1 for b0, b1 in busy):
            return from_min(s), from_min(s + duration)
    return None


class AutonomousRePlanningModule:
    def __init__(
        self,
        status: StatusManager,
        alternatives: AlternativeActivitiesModule,
        household: Household,
        forecast_lookup: ForecastLookup | None = None,
        max_attempts: int = 3,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.status = status
        self.alternatives = alternatives
        self.household = household
        self.forecast_lookup = forecast_lookup
        self.max_attempts = max_attempts
        self.clock = clock
        self.cfg = status.checker.config

    # --- öffentlich ---------------------------------------------------------------------------

    def handle(self, event: WeatherDisruption) -> ReplanOutcome:
        if self.status.event_handled(event.id):
            return ReplanOutcome(event_id=event.id, status=ChangeStatus.DUPLICATE)
        for attempt in range(1, self.max_attempts + 1):
            now = self.clock()
            itin = self.status.get(event.itinerary_id)
            ops, actions, decisions, needs_user = self._plan(itin, event, now)
            if not ops:
                return ReplanOutcome(event_id=event.id, status=ChangeStatus.NOOP, decisions=decisions,
                                     needs_user=needs_user, attempts=attempt)
            changed = sum(d.action != "unveraendert" for d in decisions)
            span = f"{event.date_from:%d.%m.}" + (f"–{event.date_to:%d.%m.}" if event.date_to != event.date_from else "")
            cs = ChangeSet(
                itinerary_id=itin.id, base_version=itin.version, operations=ops,
                reason=f"Schlechtwetter in {event.place.name} ({span}): {changed} Aktivität(en) angepasst",
                event_id=event.id, actor="autonom", external_actions=actions,
            )
            res = self.status.apply(cs, self.household.constraints, now)
            if res.status is ChangeStatus.CONFLICT:
                continue  # jemand hat den Plan gerade geändert → mit neuem Stand nochmals planen
            return ReplanOutcome(event_id=event.id, status=res.status, decisions=decisions,
                                 needs_user=needs_user, result=res, attempts=attempt)
        return ReplanOutcome(event_id=event.id, status=ChangeStatus.CONFLICT, attempts=self.max_attempts,
                             needs_user=["Der Plan wurde mehrmals gleichzeitig geändert – bitte selbst prüfen."])

    def handle_closure(self, event: ClosureDisruption) -> ReplanOutcome:
        """Ein Programmpunkt fällt aus (geschlossen, abgesagt): durch die beste Alternative ersetzen."""
        if self.status.event_handled(event.id):
            return ReplanOutcome(event_id=event.id, status=ChangeStatus.DUPLICATE)
        for attempt in range(1, self.max_attempts + 1):
            now = self.clock()
            itin = self.status.get(event.itinerary_id)
            found = itin.find_activity(event.activity_id or "")
            if not found:
                return ReplanOutcome(event_id=event.id, status=ChangeStatus.REJECTED,
                                     needs_user=["Diesen Programmpunkt gibt es im Plan nicht (mehr)."])
            day, a = found
            why = event.description or "fällt aus"
            planned = {x.title for d in itin.days for x in d.activities}
            chosen = None
            for sa in self.alternatives.suggest(place=day.base, day=day.date, start=a.start, end=a.end, original=a,
                                                exclude_titles=planned, budget_left=self._budget_left(itin), limit=5):
                new = self.alternatives.to_activity(sa, a.start, a, f"Ersatz für «{a.title}» ({why})")
                op = ReplaceActivity(activity_id=a.id, new=new)
                try:
                    trial = apply_operations(itin, [op])
                except PlanOperationError:
                    continue
                if any(p.level == "fehler" for p in self.status.checker.check(
                        itin, trial, self.household.constraints, now, {a.id})):
                    continue
                chosen = (op, new, sa)
                break
            if chosen is None:
                return ReplanOutcome(event_id=event.id, status=ChangeStatus.NOOP, needs_user=[
                    f"«{a.title}»: keine passende Alternative gefunden."])
            op, new, sa = chosen
            actions = self._booking_actions(a, datetime.min) + self._request_actions(sa, new)
            res = self.status.apply(ChangeSet(
                itinerary_id=itin.id, base_version=itin.version, operations=[op], event_id=event.id,
                reason=f"«{a.title}» {why}: ersetzt durch «{new.title}»", actor="autonom",
                external_actions=actions, acknowledged_locked=[a.id]), self.household.constraints, now)
            if res.status is ChangeStatus.CONFLICT:
                continue
            dec = ReplanDecision(activity_id=a.id, title=a.title, date=day.date, action="ersetzt", reason=why,
                                 new_activity_id=new.id, detail=f"ersetzt durch «{new.title}»")
            return ReplanOutcome(event_id=event.id, status=res.status, decisions=[dec], result=res, attempts=attempt)
        return ReplanOutcome(event_id=event.id, status=ChangeStatus.CONFLICT, attempts=self.max_attempts)

    # --- Planung ------------------------------------------------------------------------------

    def _plan(self, itin: Itinerary, event: WeatherDisruption, now: datetime):
        work = itin
        ops: list[PlanOperation] = []
        actions: list[ExternalAction] = []
        decisions: list[ReplanDecision] = []
        needs_user: list[str] = []
        for d in event.dates():
            day, fc = itin.day(d), event.forecast_for(d)
            if day is None or fc is None or not day.base.same_area(event.place):
                continue
            for a in day.activities:
                why = ", ".join(fc.problems_for(a.limits(), a.start, a.end))
                if not why or datetime.combine(d, a.start) <= now:
                    continue  # Wetter passt, oder Aktivität läuft schon / ist vorbei
                if a.locked(now):
                    decisions.append(ReplanDecision(activity_id=a.id, title=a.title, date=d,
                                                    action="unveraendert", reason=why, detail="fest gebucht"))
                    needs_user.append(f"{d:%d.%m.} «{a.title}» ist fest gebucht, aber: {why}. Bitte selbst entscheiden.")
                    continue
                chosen = None
                for step_ops, decision, step_actions in self._options(work, a, d, fc, event, why, now):
                    try:
                        trial = apply_operations(work, step_ops)
                    except PlanOperationError:
                        continue
                    if any(p.level == "fehler" for p in
                           self.status.checker.check(itin, trial, self.household.constraints, now)):
                        continue
                    chosen = (trial, step_ops, decision, step_actions)
                    break
                if chosen is None:
                    decisions.append(ReplanDecision(activity_id=a.id, title=a.title, date=d,
                                                    action="unveraendert", reason=why, detail="keine Alternative"))
                    needs_user.append(f"{d:%d.%m.} «{a.title}»: {why}, aber keine passende Alternative gefunden.")
                    continue
                work, step_ops, decision, step_actions = chosen
                ops += step_ops
                actions += step_actions
                decisions.append(decision)
        return ops, actions, decisions, needs_user

    def _options(self, work: Itinerary, a: Activity, d: date, fc: WeatherForecast,
                 event: WeatherDisruption, why: str, now: datetime) -> Iterator[tuple[list, ReplanDecision, list]]:
        base = work.day(d).base
        planned = {x.title for day in work.days for x in day.activities}
        budget_left = self._budget_left(work)
        booking_actions = self._booking_actions(a, now)

        # 1./2. Verschieben auf einen trockenen Tag
        for to_day, start, end in self._dry_slots(work, a, d, event, now):
            move = MoveActivity(activity_id=a.id, to_day=to_day, start=start, end=end)
            detail = f"verschoben auf {to_day:%d.%m.} {start:%H:%M}"
            moved_actions = [x.model_copy(update={"kind": "buchung_aendern",
                                                  "description": f"Buchung «{a.title}» auf {to_day:%d.%m.} ändern"})
                             for x in booking_actions]
            for sa in self.alternatives.suggest(place=base, day=d, start=a.start, end=a.end, original=a,
                                                forecast=fc, exclude_titles=planned, budget_left=budget_left, limit=2):
                filler = self.alternatives.to_activity(sa, a.start, a, f"Statt «{a.title}» ({why})")
                yield ([move, AddActivity(day=d, activity=filler)],
                       ReplanDecision(activity_id=a.id, title=a.title, date=d, action="verschoben_mit_ersatz",
                                      reason=why, new_activity_id=filler.id,
                                      detail=f"{detail}; heute stattdessen «{filler.title}»"),
                       moved_actions + self._request_actions(sa, filler))
            yield ([move], ReplanDecision(activity_id=a.id, title=a.title, date=d, action="verschoben",
                                          reason=why, detail=detail), moved_actions)

        # 3. Ersetzen
        for sa in self.alternatives.suggest(place=base, day=d, start=a.start, end=a.end, original=a,
                                            forecast=fc, exclude_titles=planned, budget_left=budget_left):
            new = self.alternatives.to_activity(sa, a.start, a, f"Ersatz für «{a.title}» ({why})")
            yield ([ReplaceActivity(activity_id=a.id, new=new)],
                   ReplanDecision(activity_id=a.id, title=a.title, date=d, action="ersetzt", reason=why,
                                  new_activity_id=new.id, detail=f"ersetzt durch «{new.title}»"),
                   booking_actions + self._request_actions(sa, new))

    def _dry_slots(self, work: Itinerary, a: Activity, d: date, event: WeatherDisruption,
                   now: datetime) -> Iterator[tuple[date, time, time]]:
        if self.forecast_lookup is None:
            return
        base = work.day(d).base
        bad_days = set(event.dates())
        days = [x for x in work.days if x.date not in bad_days and x.date >= now.date() and x.base.same_area(base)]
        days.sort(key=lambda x: (abs((x.date - d).days), x.date < d))  # nächster Tag zuerst, später vor früher
        for day in days:
            fc = self.forecast_lookup(day.base, day.date)
            if fc is None:
                continue  # unbekanntes Wetter: lieber nicht dorthin schieben
            slot = find_free_slot(day, a.duration_min, a.start, self.cfg)
            if slot is None or datetime.combine(day.date, slot[0]) <= now:
                continue
            if fc.problems_for(a.limits(), *slot):
                continue
            yield day.date, *slot

    def _budget_left(self, work: Itinerary) -> Decimal | None:
        cap = self.household.constraints.budget_for((work.end_date - work.start_date).days + 1)
        return None if cap is None else cap - work.total_cost()

    @staticmethod
    def _booking_actions(a: Activity, now: datetime) -> list[ExternalAction]:
        if a.booking is None or not a.booking.can_cancel(now):
            return []
        return [ExternalAction(kind="buchung_stornieren", description=f"Buchung «{a.title}» stornieren",
                               payload={"activity_id": a.id, "provider": a.booking.provider,
                                        "reference": a.booking.reference})]

    @staticmethod
    def _request_actions(sa, new: Activity) -> list[ExternalAction]:
        if not sa.candidate.booking_needed:
            return []
        return [ExternalAction(kind="buchung_anfragen", description=f"«{new.title}» reservieren",
                               payload={"activity_id": new.id, "start": new.start.isoformat()})]
