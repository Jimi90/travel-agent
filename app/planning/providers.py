"""Woher Ersatz-Aktivitäten kommen.

1. Das Schlechtwetter-Programm, das beim Planen pro Ort mit erzeugt wurde (kostet nichts).
2. Reicht das nicht (weniger als 3 Vorschläge für den Ort), fragt Kompass einmalig die KI und merkt
   sich die Antwort 30 Tage pro Ort.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from pydantic import BaseModel

from .. import llm, store
from ..models import AlternativeCandidate, Household, Itinerary, Place
from .generator import TAGS, DraftAlternative, _int, _money, _t
from .matching import match


class AltList(BaseModel):
    alternatives: list[DraftAlternative]


class PlanAlternatives:
    def __init__(self, itin: Itinerary, household: Household, allow_llm: bool = True):
        self.itin = itin
        self.household = household
        self.allow_llm = allow_llm

    def candidates(self, place: Place, day: date) -> list[AlternativeCandidate]:
        own = [a for a in self.itin.alternatives if a.place.same_area(place)]
        if len(own) >= 3 or not self.allow_llm or not llm.available():
            return own
        return own + self._from_llm(place)

    def more(self, place: Place, day: date) -> list[AlternativeCandidate]:
        """Zusätzliche Vorschläge, wenn aus dem Plan nichts passt (KI, 30 Tage gemerkt)."""
        if not self.allow_llm or not llm.available():
            return []
        own = {a.title.casefold() for a in self.itin.alternatives}
        return [a for a in self._from_llm(place) if a.title.casefold() not in own]

    def _from_llm(self, place: Place) -> list[AlternativeCandidate]:
        key = f"{place.country}:{place.name.casefold()}"
        cached = store.get("alts", key)
        if cached and datetime.fromisoformat(cached["ts"]) > datetime.now() - timedelta(days=30):
            return [AlternativeCandidate.model_validate(a) for a in cached["items"]]
        m = match(self.household)
        try:
            res = llm.ask(
                "ersatzprogramm", trip_id=self.itin.id, schema=AltList, max_tokens=6000,
                system="Du bist Kompass, ein Reiseplaner. Deutsch, Schweizer Rechtschreibung (kein ß). Nur echte Orte.",
                content=(f"Ort: {place.name} ({place.country}).\n{m.prompt_text()}\nNenne 8 wetterfeste Aktivitäten "
                         f"(60–240 Min.) für Regentage oder Ausfälle. tags nur aus: {', '.join(TAGS)}. "
                         f"Feld base = «{place.name}»."),
                fake=lambda: AltList(alternatives=[DraftAlternative(
                    base=place.name, title=f"Indoor-Markt {place.name}", category="kulinarik", exposure="indoor",
                    intensity=1, duration_min=120, cost_chf=40, tags=[], opens="", closes="")]))
        except llm.LLMError as e:
            print(f"[Ersatzprogramm] {place.name}: {e}", flush=True)
            return []
        items = []
        for a in res.alternatives:
            try:
                items.append(AlternativeCandidate(
                    title=a.title[:160], category=a.category, exposure=a.exposure, intensity=_int(a.intensity),
                    duration_min=min(720, max(15, int(a.duration_min or 60))), cost=_money(a.cost_chf), place=place,
                    tags={t.lower() for t in a.tags}, opens=_t(a.opens), closes=_t(a.closes), source="ki"))
            except ValueError:
                continue
        store.put("alts", key, {"ts": datetime.now().isoformat(), "items": [i.model_dump(mode="json") for i in items]})
        return items
