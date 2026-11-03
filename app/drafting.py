"""DraftingFeature: Nachrichten-Entwürfe für Umbuchungen, Verlängerungen und Anfragen.

Ohne KI-Schlüssel gibt es solide Vorlagen (Deutsch + Englisch). Mit KI wird der Text in der Sprache
des Empfängers geschrieben, mit deutscher Übersetzung daneben. Gesendet wird nie automatisch.
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from . import llm, store


class Draft(BaseModel):
    subject: str
    language: str
    message: str  # in der Sprache des Empfängers
    message_de: str  # deutsche Fassung zum Verstehen
    source: str = "ki"


def _d(s: str) -> str:
    try:
        return date.fromisoformat(s).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return s or "?"


def _names() -> str:
    h = store.household()
    return " & ".join(p.name for p in h.profiles if p.name)


def template(kind: str, p: dict) -> Draft:
    """Vorlage ohne KI. Englisch versteht fast jedes Hotel und jede Fähr-/Fluggesellschaft."""
    who = _names()
    if kind == "hotel_verlaengern":
        subj = f"Extension of our stay – {p.get('stay', '')}"
        en = (f"Dear {p.get('stay', 'hotel')} team,\n\nunfortunately our onward connection has been cancelled. We would like to extend our stay by {p.get('nights', 1)} night(s): "
              f"new check-out {_d(p.get('new_check_out', ''))} instead of {_d(p.get('old_check_out', ''))}.\n"
              "Could you please confirm availability and the rate? Same room if possible.\n\n"
              f"Thank you very much and kind regards,\n{who}")
        de = (f"Liebes Team von {p.get('stay', 'Hotel')},\n\nleider fällt unsere Weiterreise aus "
              f"({p.get('reason', '')}). Wir möchten um {p.get('nights', 1)} Nacht/Nächte verlängern: neue Abreise "
              f"{_d(p.get('new_check_out', ''))} statt {_d(p.get('old_check_out', ''))}.\nKönnen Sie Verfügbarkeit und "
              f"Preis bestätigen? Wenn möglich im gleichen Zimmer.\n\nVielen Dank und freundliche Grüsse\n{who}")
    elif kind == "transport_umbuchen":
        subj = f"Rebooking request {p.get('from', '')} – {p.get('to', '')}"
        en = (f"Hello,\n\nour {p.get('mode', 'connection')} from {p.get('from', '')} to {p.get('to', '')} on "
              f"{_d(p.get('old_date', ''))} ({p.get('carrier', '')}) could not take place. "
              f"Please rebook us (2 adults) to {_d(p.get('new_date', ''))} or the next possible departure, and let us "
              f"know about any fees or refunds.\n\nKind regards,\n{who}")
        de = (f"Guten Tag\n\nunsere Verbindung von {p.get('from', '')} nach {p.get('to', '')} am "
              f"{_d(p.get('old_date', ''))} ({p.get('carrier', '')}) fand nicht statt ({p.get('reason', '')}). Bitte "
              f"buchen Sie uns (2 Erwachsene) auf den {_d(p.get('new_date', ''))} oder die nächste mögliche Abfahrt um "
              f"und informieren Sie uns über Gebühren oder Rückerstattungen.\n\nFreundliche Grüsse\n{who}")
    elif kind == "buchung_aendern":
        name = p.get("stay") or p.get("title") or "our booking"
        new = f"{_d(p.get('new_check_in', p.get('new_date', '')))}" + (
            f" – {_d(p.get('new_check_out'))}" if p.get("new_check_out") else "")
        subj = f"Change of dates – {name}"
        en = (f"Hello,\n\ndue to a cancelled connection our travel plans have shifted. Could we move our booking "
              f"«{name}» to {new}? Please let us know if this is possible and about any costs.\n\nKind regards,\n{who}")
        de = (f"Guten Tag\n\nwegen einer ausgefallenen Verbindung verschiebt sich unsere Reise. Können wir unsere "
              f"Buchung «{name}» auf {new} verschieben? Bitte teilen Sie uns mit, ob das geht und was es kostet."
              f"\n\nFreundliche Grüsse\n{who}")
    elif kind in ("buchung_anfragen",):
        what = p.get("title") or f"a room in {p.get('place', '')}"
        subj = f"Request – {what}"
        when = f"{_d(p.get('check_in', ''))} – {_d(p.get('check_out', ''))}" if p.get("check_in") else p.get("start", "")
        en = (f"Hello,\n\nwe would like to request {what} for 2 adults ({when}). Is this available, and what is the "
              f"price?\n\nKind regards,\n{who}")
        de = (f"Guten Tag\n\nwir möchten {what} für 2 Erwachsene anfragen ({when}). Ist das verfügbar und was kostet "
              f"es?\n\nFreundliche Grüsse\n{who}")
    elif kind == "buchung_stornieren":
        subj = f"Cancellation – {p.get('reference') or ''}".strip(" –")
        en = (f"Hello,\n\nwe would like to cancel our booking (reference {p.get('reference') or '-'}) because of the "
              f"weather. Please confirm the cancellation and any refund.\n\nKind regards,\n{who}")
        de = (f"Guten Tag\n\nwir möchten unsere Buchung (Referenz {p.get('reference') or '-'}) wegen des Wetters "
              f"stornieren. Bitte bestätigen Sie die Stornierung und eine allfällige Rückerstattung.\n\n"
              f"Freundliche Grüsse\n{who}")
    else:
        subj, en, de = "Anfrage", f"Hello,\n\n{p.get('details', '')}\n\nKind regards,\n{who}", \
            f"Guten Tag\n\n{p.get('details', '')}\n\nFreundliche Grüsse\n{who}"
    return Draft(subject=subj, language="Englisch", message=en, message_de=de, source="vorlage")


def write(kind: str, payload: dict, language: str = "", recipient: str = "", details: str = "",
          trip_id: str | None = None) -> Draft:
    """Entwurf mit KI (Sprache des Empfängers); ohne Schlüssel die Vorlage."""
    base = template(kind, {**payload, "details": details})
    if not llm.available():
        return base
    lang = language.strip() or "die Landessprache des Empfängers, sonst Englisch"
    prompt = (f"Art: {kind}\nEmpfänger: {recipient or payload.get('stay') or payload.get('carrier') or 'unbekannt'}\n"
              f"Angaben: {payload}\nZusatz: {details or '-'}\nAbsender: {_names()} (2 Erwachsene aus der Schweiz)\n"
              f"Sprache der Nachricht: {lang}\nVorlage zur Orientierung:\n{base.message_de}")
    return llm.ask("nachricht_entwurf", trip_id=trip_id, schema=Draft, max_tokens=3000, content=prompt,
                   system=("Du schreibst kurze, höfliche, klare Nachrichten an Hotels, Fähr- und Fluggesellschaften. "
                           "Keine erfundenen Fakten (Buchungsnummern nur wenn angegeben). message in der gewünschten "
                           "Sprache, message_de als deutsche Fassung (Schweizer Rechtschreibung, kein ß). source = «ki»."),
                   fake=lambda: base.model_copy(update={"source": "testmodus"}))
