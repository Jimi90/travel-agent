"""Buchungsbestätigungen importieren: Foto/Bildschirmfoto oder eingefügter Mailtext → echte Daten im Plan.

1. KI liest die Bestätigung (Flug, Hotel, Fähre, Zug, Mietauto, Tickets) als strukturierte Daten.
2. Der Code ordnet jede Buchung dem passenden Eintrag im Plan zu (gleiche Art, gleiches Datum, ähnlicher Ort/Name)
   oder legt einen neuen an – und zeigt zuerst eine Vorschau.
3. Erst nach «Übernehmen» geht alles als EIN ChangeSet an den StatusManager (prüfbar, rückgängig machbar).
"""
from __future__ import annotations

import unicodedata
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, TypeAdapter

from . import llm, store
from .core.operations import PlanOperationError, apply_operations
from .core.plausibility import PlausibilityChecker
from .models import (
    Accommodation, Activity, AddActivity, AddStay, AddTransport, Booking, Itinerary, Place, ReplaceActivity,
    TransportSegment, UpdateStay, UpdateTransport, new_id,
)
from .models.changes import PlanOperation
from .models.common import ActivityCategory, Exposure

Kind = Literal["flug", "hotel", "faehre", "zug", "bus", "mietauto", "aktivitaet"]
MODE = {"flug": "flug", "faehre": "faehre", "zug": "zug", "bus": "bus", "mietauto": "auto"}
OPS = TypeAdapter(list[PlanOperation])


class BookedItem(BaseModel):
    kind: Kind
    title: str  # Hotelname, Aktivität oder Strecke
    provider: str  # Airline, Hotel, Fährgesellschaft, Anbieter
    reference: str  # Buchungsnummer, "" wenn keine
    number: str  # Flug-/Zugnummer, "" wenn keine
    from_place: str
    from_code: str  # IATA-Code bei Flügen, sonst ""
    to_place: str
    to_code: str
    place: str  # Ort (Hotel/Aktivität)
    start_date: str  # YYYY-MM-DD (Abflug, Anreise, Beginn)
    start_time: str  # HH:MM oder ""
    end_date: str  # YYYY-MM-DD (Ankunft, Abreise, Ende) oder ""
    end_time: str
    via: list[str]  # Umsteigeorte
    price_total: float  # 0 wenn unbekannt
    currency: str  # z. B. CHF, EUR
    cancellable: bool
    cancel_until: str  # YYYY-MM-DD oder ""
    category: Literal["wandern", "natur", "strand", "wassersport", "kultur", "museum", "stadt", "kulinarik",
                      "wellness", "entspannung", "shopping", "nachtleben"]
    exposure: Literal["indoor", "outdoor", "gemischt"]


class BookingExtract(BaseModel):
    items: list[BookedItem]
    summary: str


SYSTEM = """Du liest Buchungsbestätigungen (Flug, Hotel, Fähre, Zug, Bus, Mietauto, Tickets) und gibst die Daten
genau so zurück, wie sie in der Bestätigung stehen. Nichts erfinden: Unbekanntes als "" bzw. 0. Jede Strecke
(auch Hin- und Rückflug) und jedes Hotel als eigenes item. Daten als YYYY-MM-DD, Zeiten als HH:MM (Ortszeit wie
angegeben). summary: ein Satz auf Deutsch (Schweizer Rechtschreibung, kein ß)."""


def extract(image_b64: str | None, media_type: str, text: str, trip_id: str) -> BookingExtract:
    content: list = []
    if image_b64:
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}})
    content.append({"type": "text", "text": ("Bestätigungstext:\n" + text[:20000]) if text.strip()
                    else "Bitte die Buchung im Bild auslesen."})
    return llm.ask("buchung_lesen", system=SYSTEM, content=content, schema=BookingExtract, trip_id=trip_id,
                   max_tokens=6000, fake=lambda: _fake(trip_id))


# --- Zuordnung zum Plan ------------------------------------------------------------------------

def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().casefold().strip()


def _similar(a: str, b: str) -> bool:
    a, b = _fold(a), _fold(b)
    return bool(a and b) and (a in b or b in a or a.split()[0] == b.split()[0])


def _d(s: str) -> date | None:
    try:
        return date.fromisoformat(s.strip()[:10])
    except (ValueError, AttributeError):
        return None


def _t(s: str) -> time | None:
    try:
        hh, mm = s.strip().split(":")[:2]
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def _place(itin: Itinerary, name: str, code: str, on: date | None) -> Place:
    for d in itin.days:
        if _similar(d.base.name, name):
            return d.base
    for t in itin.transports:
        for p, c in ((t.origin, t.origin_code), (t.destination, t.destination_code)):
            if (code and c == code.upper()) or _similar(p.name, name):
                return p
    day = itin.day(on) if on else None
    country = day.base.country if day else (itin.days[0].base.country if itin.days else "CH")
    if code and code.upper() == itin.home_airport.upper():
        country = "CH"
    return Place(name=(name or code or "?")[:120], country=country)


def _money(it: BookedItem) -> tuple[Decimal | None, str]:
    if it.price_total and it.price_total > 0:
        amount = Decimal(str(round(it.price_total, 2)))
        if it.currency.upper() in ("CHF", ""):
            return amount, ""
        return None, f"Preis laut Buchung: {amount} {it.currency.upper()}"
    return None, ""


def propose(itin: Itinerary, ext: BookingExtract) -> tuple[list, list[str], list[str], list[str]]:
    """→ (Operationen, Vorschau-Zeilen, Hinweise, bewusst geänderte feste Aktivitäten). Ändert nichts."""
    ops: list = []
    lines: list[str] = []
    warn: list[str] = []
    ack: list[str] = []
    used_stays, used_segs = set(), set()
    for it in ext.items:
        start = _d(it.start_date)
        if start is None:
            warn.append(f"«{it.title}»: kein Datum erkannt – übersprungen")
            continue
        cost, cost_note = _money(it)
        ref = it.reference.strip() or None
        if it.kind == "hotel":
            end = _d(it.end_date) or start + timedelta(days=1)
            if end <= start:
                warn.append(f"«{it.title}»: Abreise vor Anreise – übersprungen")
                continue
            cand = [s for s in itin.stays if s.id not in used_stays and s.check_in < end and start < s.check_out]
            stay = next((s for s in cand if _similar(s.name, it.title) or _similar(s.place.name, it.place)), None) \
                or (cand[0] if cand else None)
            note = "; ".join(x for x in (cost_note, f"Stornierbar bis {it.cancel_until}" if it.cancel_until else "") if x)
            if stay:
                used_stays.add(stay.id)
                ops.append(UpdateStay(stay_id=stay.id, name=it.title[:160], check_in=start, check_out=end, cost=cost,
                                      confirmed=True, provider=it.provider or it.title, reference=ref,
                                      note=note or None))
                lines.append(f"Unterkunft «{stay.name}» → «{it.title}», {start:%d.%m.}–{end:%d.%m.}, gebucht"
                             + (f" (Ref. {ref})" if ref else ""))
            else:
                p = _place(itin, it.place or it.title, "", start)
                ops.append(AddStay(stay=Accommodation(name=it.title[:160], place=p, check_in=start, check_out=end,
                                                      cost=cost or Decimal("0"), confirmed=True, note=note or None,
                                                      booking=Booking(provider=(it.provider or it.title)[:80],
                                                                      reference=ref))))
                lines.append(f"Neue Unterkunft «{it.title}», {start:%d.%m.}–{end:%d.%m.}, gebucht")
        elif it.kind in MODE:
            mode = MODE[it.kind]
            t0 = _t(it.start_time) or time(9)
            dep = datetime.combine(start, t0)
            arr_t = _t(it.end_time)
            arr = datetime.combine(_d(it.end_date) or start, arr_t) if arr_t else dep + timedelta(hours=2)
            if arr <= dep:
                arr += timedelta(days=1)
            if not arr_t:
                warn.append(f"«{it.title}»: keine Ankunftszeit erkannt – 2 Std. angenommen")
            cand = [t for t in itin.transports if t.id not in used_segs and t.mode == mode and t.depart.date() == start]
            fc, tc = it.from_code.strip().upper(), it.to_code.strip().upper()
            seg = next((t for t in cand if (fc and t.origin_code == fc) or (tc and t.destination_code == tc)
                        or _similar(t.origin.name, it.from_place) or _similar(t.destination.name, it.to_place)), None) \
                or (cand[0] if len(cand) == 1 else None)
            label = f"{it.from_place or fc} → {it.to_place or tc}"
            if seg:
                used_segs.add(seg.id)
                ops.append(UpdateTransport(segment_id=seg.id, depart=dep, arrive=arr, carrier=it.provider or None,
                                           number=it.number or None, via=[v for v in it.via if v] or None,
                                           origin_code=fc or None, destination_code=tc or None, cost=cost,
                                           confirmed=True, reference=ref, note=cost_note))  # "" löscht «Schätzung»
                lines.append(f"{label}: {dep:%d.%m. %H:%M} – {arr:%H:%M}{' · ' + it.number if it.number else ''}, gebucht")
            else:
                seg_new = TransportSegment(
                    mode=mode, origin=_place(itin, it.from_place, fc, start), destination=_place(itin, it.to_place, tc, start),
                    depart=dep, arrive=arr, carrier=it.provider or None, number=it.number or None,
                    via=[v for v in it.via if v], origin_code=fc if len(fc) == 3 else None,
                    destination_code=tc if len(tc) == 3 else None, cost=cost or Decimal("0"), estimated=False,
                    booking=Booking(provider=(it.provider or mode)[:80], reference=ref), note=cost_note or None)
                ops.append(AddTransport(segment=seg_new))
                lines.append(f"Neue Verbindung {label}: {dep:%d.%m. %H:%M}, gebucht")
        else:  # Aktivität / Tickets
            day = itin.day(start)
            if day is None:
                warn.append(f"«{it.title}» am {start:%d.%m.} liegt ausserhalb der Reise – übersprungen")
                continue
            s = _t(it.start_time) or time(10)
            e = _t(it.end_time) or time(min(22, s.hour + 2), s.minute)
            booking = Booking(provider=(it.provider or it.title)[:80], reference=ref, cancellable=it.cancellable,
                              cancel_deadline=datetime.combine(_d(it.cancel_until), time(23, 59))
                              if _d(it.cancel_until) else None)
            match = next((a for a in day.activities if _similar(a.title, it.title)), None)
            if match:
                ops.append(ReplaceActivity(activity_id=match.id, new=match.model_copy(update={
                    "booking": booking, "cost": cost if cost is not None else match.cost})))
                ack.append(match.id)
                lines.append(f"«{match.title}» am {start:%d.%m.}: Reservation eingetragen")
            else:
                ops.append(AddActivity(day=start, activity=Activity(
                    title=it.title[:160], category=ActivityCategory(it.category), exposure=Exposure(it.exposure),
                    start=s, end=e if e > s else time(min(23, s.hour + 1), s.minute), place=day.base,
                    cost=cost or Decimal("0"), booking=booking, origin="nutzer")))
                lines.append(f"Neu am {start:%d.%m.}: «{it.title}» {s:%H:%M}, gebucht")
    return ops, lines, warn, ack


def preview(itin: Itinerary, ext: BookingExtract, now: datetime) -> dict:
    ops, lines, warn, ack = propose(itin, ext)
    problems: list[str] = []
    if ops:
        try:
            after = apply_operations(itin, ops)
            k = store.household().constraints.model_copy(update={"budget_total_max": None, "budget_per_day_max": None})
            problems = [p.message for p in PlausibilityChecker().check(itin, after, k, now, set(ack))
                        if p.level == "fehler"]
        except PlanOperationError as e:
            problems = [str(e)]
    token = new_id()
    store.put("import", token, {"trip_id": itin.id, "version": itin.version, "ack": ack,
                                "ops": OPS.dump_python(ops, mode="json"), "summary": ext.summary})
    return {"token": token, "summary": ext.summary, "lines": lines, "warnings": warn, "problems": problems,
            "can_apply": bool(ops) and not problems}


def pending(token: str) -> tuple[dict, list]:
    data = store.get("import", token)
    if not data:
        raise KeyError(token)
    return data, OPS.validate_python(data["ops"])


def _fake(trip_id: str) -> BookingExtract:
    from .service import sm  # nur Testmodus
    itin = sm().get(trip_id)
    items = []
    flight = next((t for t in itin.transports if t.mode == "flug"), None)
    if flight:
        items.append(BookedItem(
            kind="flug", title="Hinflug", provider="Testair", reference="ABC123", number="TA 123",
            from_place=flight.origin.name, from_code=flight.origin_code or "ZRH", to_place=flight.destination.name,
            to_code=flight.destination_code or "LIS", place="", start_date=flight.depart.date().isoformat(),
            start_time="06:45", end_date=flight.depart.date().isoformat(), end_time="09:05", via=[], price_total=412.5,
            currency="CHF", cancellable=False, cancel_until="", category="stadt", exposure="indoor"))
    if itin.stays:
        s = itin.stays[0]
        items.append(BookedItem(
            kind="hotel", title="Hotel Testpalast", provider="Booking.com", reference="987654", number="",
            from_place="", from_code="", to_place="", to_code="", place=s.place.name,
            start_date=s.check_in.isoformat(), start_time="", end_date=s.check_out.isoformat(), end_time="",
            via=[], price_total=300, currency="EUR", cancellable=True, cancel_until=s.check_in.isoformat(),
            category="entspannung", exposure="indoor"))
    return BookingExtract(items=items, summary="Testmodus: Flug und Hotel erkannt.")
