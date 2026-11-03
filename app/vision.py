"""Bilderkennung vor Ort: Speisekarten, Schilder, Aushänge fotografieren → übersetzt und erklärt.

Bei Schwangerschaft markiert Kompass Gerichte, die man in der Schwangerschaft üblicherweise meidet –
als Hinweis, nicht als medizinische Beratung.
"""
from __future__ import annotations

import base64
from typing import Literal

from pydantic import BaseModel

from . import llm, store

MEDIA = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_BYTES = 4_500_000


class VisionItem(BaseModel):
    original: str
    translation: str
    explanation: str
    price: str
    warnings: list[str]


class VisionResult(BaseModel):
    kind: Literal["speisekarte", "schild", "dokument", "sonstiges"]
    language: str
    title: str
    items: list[VisionItem]
    summary: str


def analyse(image_b64: str, media_type: str, question: str = "", trip_id: str | None = None) -> VisionResult:
    if media_type not in MEDIA:
        raise llm.LLMError("Bitte ein Foto (JPEG, PNG oder WebP) wählen.")
    try:
        raw = base64.b64decode(image_b64, validate=True)
    except ValueError:
        raise llm.LLMError("Das Bild ist beschädigt.")
    if len(raw) > MAX_BYTES:
        raise llm.LLMError("Das Bild ist zu gross (max. 4.5 MB).")
    h = store.household()
    preg = h.constraints.pregnancy is not None
    rules = ("Eine Reisende ist schwanger: Bei Gerichten in warnings knapp vermerken, wenn sie typischerweise "
             "gemieden werden (roher Fisch/Fleisch, Rohmilchkäse, rohe Eier, Alkohol, viel Koffein) – sachlich, "
             "ohne Panik. Unklare Fälle als «nachfragen» markieren. " if preg else "")
    langs = ", ".join(sorted({l for p in h.profiles for l in p.languages})) or "de"
    system = ("Du bist Kompass, ein Reisehelfer. Lies den Text im Bild genau und übersetze ihn ins Deutsche "
              "(Schweizer Rechtschreibung, kein ß). Bei Speisekarten: jedes Gericht als eigenes item mit kurzer "
              "Erklärung, was es ist, und dem Preis wie angegeben. Bei Schildern: Bedeutung und was zu tun ist. "
              "Nichts erfinden: Unlesbares als «unleserlich» kennzeichnen. " + rules +
              f"Die Reisenden sprechen: {langs}.")
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
        {"type": "text", "text": question.strip() or "Bitte übersetzen und erklären."},
    ]
    return llm.ask("bild_uebersetzen", system=system, content=content, schema=VisionResult, trip_id=trip_id,
                   max_tokens=8000, fake=lambda: _fake(preg))


def _fake(preg: bool) -> VisionResult:
    return VisionResult(kind="speisekarte", language="Portugiesisch", title="Speisekarte (Testmodus)", items=[
        VisionItem(original="Bacalhau à Brás", translation="Stockfisch nach Brás-Art",
                   explanation="Stockfisch mit Kartoffelstroh und Ei", price="14.50 €",
                   warnings=["Ei meist weich – nachfragen"] if preg else []),
        VisionItem(original="Ameijoas à Bulhão Pato", translation="Venusmuscheln in Knoblauch-Koriander-Sauce",
                   explanation="Muscheln, kurz gegart", price="16 €",
                   warnings=["Meeresfrüchte: gut durchgegart bestellen"] if preg else []),
    ], summary="Testmodus: kein echtes Bild ausgewertet.")
