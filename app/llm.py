"""KI-Aufrufe (Claude) mit strukturierten Antworten, Kostenzählung, Kostenbremse und Testmodus.

Jeder Aufruf liefert ein geprüftes Pydantic-Objekt zurück (``output_format``) – nie freien Text,
den man selbst zerlegen müsste. Kosten werden pro Aufruf in ``store.costs`` notiert.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

from . import config, store

T = TypeVar("T", bound=BaseModel)

# Server-seitige Ausweichlösung, falls ein Modell eine Anfrage ablehnt (Claude API, Opus/Sonnet 5.5).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}


class LLMError(Exception):
    """Fehler mit Text, der so in der Oberfläche angezeigt werden kann."""


def available() -> bool:
    return config.FAKE_MODE or bool(store.anthropic_key())


def cost_usd(model: str, tin: int, tout: int, searches: int = 0) -> float:
    pin, pout = config.PRICES.get(model, config.PRICES["claude-opus-5-5"])
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
    """Fragt Claude und gibt eine geprüfte Antwort vom Typ ``schema`` zurück."""
    if config.FAKE_MODE:
        result = fake()
        store.add_cost(purpose, "testmodus", 0.0, 0, 0, trip_id)
        return schema.model_validate(result.model_dump())
    key = store.anthropic_key()
    if not key:
        raise LLMError("Kein Claude-Schlüssel hinterlegt (Einstellungen → Claude-Schlüssel).")
    spent = store.spent_since(24)
    if spent >= config.DAILY_BUDGET_USD:
        raise LLMError(f"Kostenbremse: In den letzten 24 Std. wurden schon {spent:.2f} USD ausgegeben "
                       f"(Grenze {config.DAILY_BUDGET_USD:.2f} USD).")

    import anthropic  # erst hier laden: Tests im Testmodus brauchen das Paket nicht

    s = store.settings()
    model = s["model"] if s["model"] in config.PRICES else config.MODEL
    client = anthropic.Anthropic(api_key=key, timeout=600, max_retries=2)
    kwargs: dict = dict(
        model=model, max_tokens=max_tokens, system=system, output_format=schema,
        output_config={"effort": s.get("effort") or config.EFFORT},
    )
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
        raise LLMError("Der Claude-Schlüssel wurde abgelehnt. Bitte in den Einstellungen prüfen.")
    except anthropic.RateLimitError:
        raise LLMError("Claude ist gerade ausgelastet (Tempolimit). Bitte in einigen Minuten nochmals versuchen.")
    except anthropic.BadRequestError as e:
        raise LLMError(f"Claude hat die Anfrage nicht angenommen: {getattr(e, 'message', e)}")
    except anthropic.APIStatusError as e:
        raise LLMError(f"Claude-Fehler {e.status_code}. Bitte später nochmals versuchen.")
    except anthropic.APIConnectionError:
        raise LLMError("Keine Verbindung zu Claude.")
    finally:
        if tin or tout:
            store.add_cost(purpose, model, cost_usd(model, tin, tout, searches), tin, tout, trip_id)

    if resp.stop_reason == "refusal":
        raise LLMError("Claude hat diese Anfrage abgelehnt.")
    if resp.stop_reason == "max_tokens":
        raise LLMError("Die Antwort wurde zu lang und ist abgeschnitten. Bitte kürzer anfragen (z. B. weniger Tage).")
    if resp.parsed_output is None:
        raise LLMError("Claude hat keine verwertbare Antwort geliefert.")
    return resp.parsed_output
