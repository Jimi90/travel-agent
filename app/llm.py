"""KI-Aufrufe mit strukturierten Antworten: Gemini und Groq (gratis) sowie Claude (kostenpflichtig, optional).

Aufgabenteilung (Einstellung «prefer» = gratis):
- Grosse Aufgaben (Reiseplan, Zielvorschläge, Foto-Übersetzung): Gemini → Groq → Claude
  (Gemini kann Bilder lesen, schreibt lange Antworten und hat das grössere Gratis-Kontingent;
   Groq kann keine Bilder und hat ein kleines Minutenkontingent)
- Kleine Aufgaben (Nachrichten-Entwurf, Ersatzprogramm): Groq → Gemini → Claude (Groq ist am schnellsten)
Mit «prefer» = claude kommt Claude zuerst. Fällt ein Anbieter aus (Limit, Überlastung, Fehler, ungültige
Antwort), übernimmt der nächste. Jede Antwort wird gegen das Pydantic-Schema geprüft.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from . import config, store

T = TypeVar("T", bound=BaseModel)

# Server-seitige Ausweichlösung, falls ein Claude-Modell eine Anfrage ablehnt (Opus/Sonnet 5.5).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
HEAVY = {"reiseplan", "zielvorschlaege", "bild_uebersetzen"}
LABEL = {"claude": "Claude", "gemini": "Gemini", "groq": "Groq"}


class LLMError(Exception):
    """Fehler mit Text, der so in der Oberfläche angezeigt werden kann."""


class _Skip(Exception):
    """Dieser Anbieter kann gerade nicht – den nächsten probieren."""


def _has_key(prov: str) -> bool:
    return bool(store.key("anthropic" if prov == "claude" else prov))


def available() -> bool:
    return config.FAKE_MODE or any(_has_key(p) for p in ("claude", "gemini", "groq"))


def route(purpose: str, has_image: bool = False) -> list[str]:
    """Reihenfolge der Anbieter für eine Aufgabe (nur solche mit Schlüssel)."""
    free = ["gemini", "groq"] if purpose in HEAVY else ["groq", "gemini"]
    order = free + ["claude"] if store.settings().get("prefer", "gratis") == "gratis" else ["claude"] + free
    if has_image:
        order = [p for p in order if p == "claude" or config.FREE_PROVIDERS[p]["vision"]]
    return [p for p in order if _has_key(p)]


def cost_usd(model: str, tin: int, tout: int, searches: int = 0) -> float:
    pin, pout = config.PRICES.get(model, (0.0, 0.0))
    return round(tin / 1e6 * pin + tout / 1e6 * pout + searches * config.WEB_SEARCH_USD, 5)


def ask(
    purpose: str,
    *,
    system: str,
    content: str | list,
    schema: type[T],
    fake: Callable[[], T],
    trip_id: str | None = None,
    max_tokens: int = 16000,
    web_search: bool = False,
) -> T:
    """Fragt die KI und gibt eine geprüfte Antwort vom Typ ``schema`` zurück."""
    if config.FAKE_MODE:
        result = fake()
        store.add_cost(purpose, "testmodus", 0.0, 0, 0, trip_id)
        return schema.model_validate(result.model_dump())
    has_image = isinstance(content, list) and any(b.get("type") == "image" for b in content)
    order = route(purpose, has_image)
    if not order:
        raise LLMError("Kein passender KI-Schlüssel hinterlegt (Profil → KI)." if has_image and _has_key("groq")
                       else "Kein KI-Schlüssel hinterlegt (Profil → KI: Gemini, Groq oder Claude).")
    reasons = []
    for prov in order:
        try:
            if prov == "claude":
                return _claude(purpose, system, content, schema, trip_id, max_tokens, web_search)
            return _compat(prov, purpose, system, content, schema, trip_id, max_tokens)
        except _Skip as e:
            reasons.append(f"{LABEL[prov]}: {e}")
            print(f"[KI] {LABEL[prov]} für «{purpose}» nicht möglich: {e}", flush=True)
    if len(reasons) == 1:
        raise LLMError(reasons[0])
    raise LLMError("Alle KI-Anbieter sind ausgefallen – " + " · ".join(reasons))


# --- Gemini / Groq (OpenAI-kompatibel) ----------------------------------------------------------

def _to_openai(content: str | list) -> str | list:
    """Claude-Inhaltsblöcke → OpenAI-Format (Bilder als data:-Adresse)."""
    if isinstance(content, str):
        return content
    out = []
    for b in content:
        if b.get("type") == "image":
            src = b["source"]
            out.append({"type": "image_url", "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
        elif b.get("type") == "text":
            out.append({"type": "text", "text": b["text"]})
    return out


def _json_text(text: str) -> str:
    """Code-Zäune und Text um das JSON herum entfernen."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    if not t.startswith("{"):
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            t = t[a:b + 1]
    return t


def _post(url: str, payload: dict, key: str, timeout: int = 300) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "content-type": "application/json", "accept": "application/json", "authorization": f"Bearer {key}",
        # Cloudflare (vor Groq) blockt den Standard-Absender «Python-urllib» mit Fehler 1010
        "user-agent": f"Kompass/{config.APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _api_msg(body: str) -> str:
    try:
        j = json.loads(body)
        j = j[0] if isinstance(j, list) and j else j
        e = j.get("error", j)
        return str(e.get("message") if isinstance(e, dict) else e)[:200]
    except (ValueError, AttributeError):
        return re.sub(r"\s+", " ", body)[:200] or "unbekannter Fehler"


def _compat(prov: str, purpose: str, system: str, content, schema: type[T], trip_id, max_tokens: int) -> T:
    c = config.FREE_PROVIDERS[prov]
    key = store.key(prov)
    schema_txt = json.dumps(schema.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
    sys = (f"{system}\n\nAntworte ausschliesslich mit einem JSON-Objekt (kein Text davor oder danach), "
           f"das genau diesem JSON-Schema entspricht:\n{schema_txt}")
    messages = [{"role": "system", "content": sys}, {"role": "user", "content": _to_openai(content)}]
    payload = {"model": c["model"], "messages": messages, "max_tokens": max_tokens,
               "response_format": {"type": "json_object"}, "reasoning_effort": "low"}
    url = c["base_url"].rstrip("/") + "/chat/completions"
    tin = tout = 0
    for attempt in range(3):
        try:
            data = _post(url, payload, key)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:800]
            if e.code == 400 and any(k in payload for k in ("response_format", "reasoning_effort")) and \
                    re.search(r"response_format|reasoning|json", body, re.I):
                payload.pop("reasoning_effort", None)  # nicht jedes Modell kennt diese Felder
                if "reasoning" not in body.lower():
                    payload.pop("response_format", None)
                continue
            if e.code in (401, 403):
                raise _Skip("Schlüssel abgelehnt – bitte prüfen")
            if e.code == 429:
                daily = re.search(r"per day|perday|daily|quota", body, re.I)
                raise _Skip("Tageslimit erreicht" if daily else "Tempolimit (zu viele Anfragen)")
            if e.code == 413:
                raise _Skip("Anfrage zu gross für das Gratis-Kontingent")
            if e.code in (500, 502, 503, 529) and attempt == 0:
                time.sleep(3)
                continue
            raise _Skip(f"Fehler {e.code}: {_api_msg(body)}")
        except (urllib.error.URLError, TimeoutError) as e:
            raise _Skip(f"nicht erreichbar ({type(e).__name__})")
        except ValueError:
            raise _Skip("unlesbare Antwort")
        u = data.get("usage") or {}
        tin += u.get("prompt_tokens", 0) or 0
        tout += u.get("completion_tokens", 0) or 0
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            raise _Skip("unerwartete Antwort")
        if choice.get("finish_reason") == "length":
            store.add_cost(purpose, f"{prov}:{c['model']}", 0.0, tin, tout, trip_id)
            raise _Skip("Antwort zu lang und abgeschnitten")
        try:
            result = schema.model_validate_json(_json_text(text))
        except (ValidationError, ValueError) as e:
            if attempt < 2:  # einmal mit Fehlerhinweis korrigieren lassen
                err = e.errors()[0] if isinstance(e, ValidationError) else {"msg": str(e)[:200]}
                messages += [{"role": "assistant", "content": text[:20000]},
                             {"role": "user", "content": f"Das JSON passt nicht zum Schema ({err.get('loc', '')}: "
                                                         f"{err.get('msg')}). Bitte vollständig und korrekt neu senden."}]
                continue
            store.add_cost(purpose, f"{prov}:{c['model']}", 0.0, tin, tout, trip_id)
            raise _Skip("Antwort passte nicht zum erwarteten Format")
        store.add_cost(purpose, f"{prov}:{c['model']}", 0.0, tin, tout, trip_id)
        return result
    raise _Skip("keine gültige Antwort")


# --- Claude ------------------------------------------------------------------------------

def _claude(purpose, system, content, schema: type[T], trip_id, max_tokens, web_search) -> T:
    spent = store.spent_since(24)
    if spent >= config.DAILY_BUDGET_USD:
        raise _Skip(f"Kostenbremse erreicht ({spent:.2f} von {config.DAILY_BUDGET_USD:.2f} USD in 24 Std.)")

    import anthropic  # erst hier laden: Tests im Testmodus brauchen das Paket nicht

    s = store.settings()
    model = s["model"] if s["model"] in config.PRICES else config.MODEL
    client = anthropic.Anthropic(api_key=store.anthropic_key(), timeout=600, max_retries=2)
    kwargs: dict = dict(model=model, max_tokens=max_tokens, system=system, output_format=schema,
                        output_config={"effort": s.get("effort") or config.EFFORT})
    if model in FALLBACK_MODELS:
        kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
    if web_search:
        kwargs["tools"] = [WEB_SEARCH_TOOL]
    messages: list = [{"role": "user", "content": content}]
    tin = tout = searches = 0
    try:
        for _ in range(4):  # Websuche kann die Antwort unterbrechen («pause_turn») – dann weitermachen
            resp = client.beta.messages.parse(messages=messages, **kwargs)
            u = resp.usage
            tin += (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0) \
                + (getattr(u, "cache_read_input_tokens", 0) or 0)
            tout += u.output_tokens or 0
            stu = getattr(u, "server_tool_use", None)
            searches += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
            if resp.stop_reason != "pause_turn":
                break
            messages = [*messages, {"role": "assistant", "content": resp.content}]
    except anthropic.AuthenticationError:
        raise _Skip("Schlüssel abgelehnt – bitte prüfen")
    except anthropic.RateLimitError:
        raise _Skip("Tempolimit (zu viele Anfragen)")
    except anthropic.BadRequestError as e:
        raise _Skip(f"Anfrage nicht angenommen: {getattr(e, 'message', e)}")
    except anthropic.APIStatusError as e:
        raise _Skip(f"Fehler {e.status_code}")
    except anthropic.APIConnectionError:
        raise _Skip("nicht erreichbar")
    finally:
        if tin or tout:
            store.add_cost(purpose, model, cost_usd(model, tin, tout, searches), tin, tout, trip_id)

    if resp.stop_reason == "refusal":
        raise _Skip("Anfrage abgelehnt")
    if resp.stop_reason == "max_tokens":
        raise _Skip("Antwort zu lang und abgeschnitten")
    if resp.parsed_output is None:
        raise _Skip("keine verwertbare Antwort")
    return resp.parsed_output
