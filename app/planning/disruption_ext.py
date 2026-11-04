"""ExtendedDisruptionHandler: mehrtägige Ausfälle (Fähre/Flug fährt X Tage nicht).

Ein einziges ChangeSet (ganz oder gar nicht):
  1. Plan ab dem Abfahrtstag um X Tage nach hinten schieben (Tage, Unterkünfte, Verbindungen, Reiseende)
  2. Unterkunft am Ort, wo man festsitzt, verlängern – oder eine neue vormerken, falls keine passt
  3. Die gewonnenen Tage mit wetterfestem Programm vom Ort füllen
  4. Für alles, was ausserhalb der App passieren muss (Hotel verlängern, Fähre/Flug umbuchen, spätere
     Buchungen verschieben), einen Auftrag mit Bestätigung anlegen – mit Nachrichten-Entwurf.
Das Budget wird hier nicht als Sperre geprüft (man sitzt ohnehin fest), sondern nur gemeldet.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from ..core.status_manager import StatusManager
from ..models import (
    Accommodation, AddActivity, AddStay, ChangeResult, ChangeSet, ChangeStatus, ExternalAction, Household,
    ShiftSchedule, TransportDisruption,
)
from ..models.common import Model
from .alternatives import AlternativeActivitiesModule

MODE = {"flug": "Flug", "faehre": "Fähre", "zug": "Zug", "bus": "Bus", "auto": "Mietauto"}


class DisruptionOutcome(Model):
    event_id: str
    status: ChangeStatus
    summary: list[str] = []
    warnings: list[str] = []
    result: ChangeResult | None = None

    def message(self) -> str:
        return "\n".join(self.summary + self.warnings)


class ExtendedDisruptionHandler:
    def __init__(self, status: StatusManager, alternatives: AlternativeActivitiesModule, household: Household,
                 clock: Callable[[], datetime] = datetime.now):
        self.status = status
        self.alternatives = alternatives
        self.household = household
        self.clock = clock

    def handle(self, ev: TransportDisruption) -> DisruptionOutcome:
        if self.status.event_handled(ev.id):
            return DisruptionOutcome(event_id=ev.id, status=ChangeStatus.DUPLICATE)
        for _ in range(3):
            itin = self.status.get(ev.itinerary_id)
            seg = next((t for t in itin.transports if t.id == ev.segment_id), None)
            if seg is None:
                return DisruptionOutcome(event_id=ev.id, status=ChangeStatus.REJECTED,
                                         warnings=["Diese Verbindung gibt es im Plan nicht (mehr)."])
            now = self.clock()
            n = ev.delay_days
            start = seg.depart.date()
            if start < itin.start_date or start > itin.end_date:
                return DisruptionOutcome(event_id=ev.id, status=ChangeStatus.REJECTED,
                                         warnings=["Die Verbindung liegt ausserhalb der Reisedaten."])
            what = f"{MODE.get(seg.mode, seg.mode)} {seg.origin.name} → {seg.destination.name}"
            prev = itin.day(start - timedelta(days=1))
            stuck = prev.base if prev and prev.base.same_area(seg.origin) else seg.origin
            ops: list = [ShiftSchedule(from_date=start, days=n, gap_base=stuck)]
            actions: list[ExternalAction] = []
            summary = [f"{what} am {start:%d.%m.} fällt aus – {n} Tag(e) Verzögerung.",
                       f"Alle Tage ab {start:%d.%m.} rücken um {n} Tag(e) nach hinten; neues Reiseende "
                       f"{itin.end_date + timedelta(days=n):%d.%m.%Y}."]
            warnings: list[str] = []

            # Unterkunft am Ort, wo man festsitzt
            covering = next((s for s in itin.stays if s.check_in < start <= s.check_out
                             and s.place.same_area(stuck)), None)
            if covering:
                new_out = covering.check_out + timedelta(days=n)
                summary.append(f"«{covering.name}» wird bis {new_out:%d.%m.} verlängert.")
                actions.append(ExternalAction(
                    kind="hotel_verlaengern",
                    description=f"«{covering.name}» um {n} Nacht/Nächte verlängern (neue Abreise {new_out:%d.%m.})",
                    payload={"stay": covering.name, "place": stuck.name, "check_in": covering.check_in.isoformat(),
                             "old_check_out": covering.check_out.isoformat(), "new_check_out": new_out.isoformat(),
                             "nights": n, "reason": f"{what} fällt aus"}))
            else:
                per_night = _avg_night(itin)
                new = Accommodation(name=f"Unterkunft {stuck.name} (gesucht)", place=stuck, check_in=start,
                                    check_out=start + timedelta(days=n), cost=round(per_night * n, 2),
                                    confirmed=False, note=f"Wegen Ausfall: {what}")
                ops.append(AddStay(stay=new))
                summary.append(f"Für {n} Nacht/Nächte in {stuck.name} wird eine Unterkunft gesucht.")
                actions.append(ExternalAction(
                    kind="buchung_anfragen", description=f"Unterkunft in {stuck.name} für {n} Nacht/Nächte finden",
                    payload={"place": stuck.name, "check_in": start.isoformat(),
                             "check_out": (start + timedelta(days=n)).isoformat(), "reason": f"{what} fällt aus"}))

            # Die ausgefallene Verbindung umbuchen
            actions.append(ExternalAction(
                kind="transport_umbuchen", description=f"{what} auf den {start + timedelta(days=n):%d.%m.} umbuchen",
                payload={"segment_id": seg.id, "mode": seg.mode, "from": seg.origin.name, "to": seg.destination.name,
                         "old_date": start.isoformat(), "new_date": (start + timedelta(days=n)).isoformat(),
                         "carrier": seg.carrier or "", "reason": ev.description or "Ausfall"}))

            # Spätere Buchungen, die mitverschoben werden
            for s in itin.stays:
                if s.check_in >= start and (s.confirmed or s.booking):
                    actions.append(ExternalAction(
                        kind="buchung_aendern",
                        description=f"«{s.name}» auf {s.check_in + timedelta(days=n):%d.%m.}–"
                                    f"{s.check_out + timedelta(days=n):%d.%m.} verschieben",
                        payload={"stay": s.name, "place": s.place.name, "old_check_in": s.check_in.isoformat(),
                                 "new_check_in": (s.check_in + timedelta(days=n)).isoformat(),
                                 "new_check_out": (s.check_out + timedelta(days=n)).isoformat()}))
            for t in itin.transports:
                if t.id != seg.id and t.depart.date() >= start and (t.booking or not t.estimated):
                    actions.append(ExternalAction(
                        kind="transport_umbuchen",
                        description=f"{MODE.get(t.mode, t.mode)} {t.origin.name} → {t.destination.name} auf "
                                    f"{(t.depart + timedelta(days=n)):%d.%m.} umbuchen",
                        payload={"segment_id": t.id, "mode": t.mode, "from": t.origin.name, "to": t.destination.name,
                                 "old_date": t.depart.date().isoformat(),
                                 "new_date": (t.depart + timedelta(days=n)).date().isoformat(),
                                 "carrier": t.carrier or "", "reason": f"Folge von: {what}"}))
            unbooked = [t for t in itin.transports if t.id != seg.id and t.depart.date() >= start and t.estimated
                        and not t.booking]
            if unbooked:
                summary.append(f"{len(unbooked)} weitere (noch nicht gebuchte) Verbindung(en) wurden mitverschoben.")

            # Feste/gebuchte Aktivitäten auf verschobenen Tagen
            ack = []
            for d in itin.days:
                if d.date < start:
                    continue
                for a in d.activities:
                    if a.locked(now):
                        ack.append(a.id)
                        actions.append(ExternalAction(
                            kind="buchung_aendern",
                            description=f"Reservation «{a.title}» auf {d.date + timedelta(days=n):%d.%m.} verschieben",
                            payload={"activity_id": a.id, "title": a.title, "old_date": d.date.isoformat(),
                                     "new_date": (d.date + timedelta(days=n)).isoformat()}))

            # Gewonnene Tage füllen
            planned = {a.title for d in itin.days for a in d.activities}
            for i in range(n):
                d = start + timedelta(days=i)
                t0 = time(10)
                for _ in range(2):
                    sug = self.alternatives.suggest(place=stuck, day=d, start=t0, end=time(18), forecast=None,
                                                    exclude_titles=planned, limit=1)
                    if not sug:
                        break
                    act = self.alternatives.to_activity(sug[0], t0, None, f"Programm während des Ausfalls ({what})")
                    ops.append(AddActivity(day=d, activity=act))
                    planned.add(act.title)
                    t0 = time(min(17, act.end.hour + 1), 0)

            k = self.household.constraints
            res = self.status.apply(
                ChangeSet(itinerary_id=itin.id, base_version=itin.version, operations=ops,
                          reason=f"Ausfall: {what} am {start:%d.%m.} ({n} Tag/e)", event_id=ev.id, actor="autonom",
                          external_actions=actions, acknowledged_locked=ack),
                k.model_copy(update={"budget_total_max": None, "budget_per_day_max": None}), now)
            if res.status is ChangeStatus.CONFLICT:
                continue
            if res.status is ChangeStatus.APPLIED:
                after = self.status.get(itin.id)
                cap = k.budget_for_trip(after)
                if cap and after.total_cost() > cap:
                    warnings.append(f"Geschätzte Kosten jetzt {after.total_cost():.0f} CHF – über dem Budget "
                                    f"({cap:.0f} CHF).")
                total_days = (after.end_date - after.start_date).days + 1
                if total_days > k.duration_days_max:
                    warnings.append(f"Die Reise dauert jetzt {total_days} Tage (Vorgabe höchstens {k.duration_days_max}).")
                if ack:
                    warnings.append(f"{len(ack)} feste Reservation(en) müssen verschoben werden – siehe Aufträge.")
                warnings += [p.message for p in res.problems if p.level == "warnung"]
            else:
                warnings += [p.message for p in res.problems]
            return DisruptionOutcome(event_id=ev.id, status=res.status, summary=summary, warnings=warnings, result=res)
        return DisruptionOutcome(event_id=ev.id, status=ChangeStatus.CONFLICT,
                                 warnings=["Der Plan wurde mehrmals gleichzeitig geändert – bitte nochmals versuchen."])


def _avg_night(itin) -> Decimal:
    nights = sum((s.check_out - s.check_in).days for s in itin.stays)
    cost = sum((s.cost for s in itin.stays), Decimal("0"))
    return (cost / nights) if nights else Decimal("0")
