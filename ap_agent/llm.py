"""One place that talks to Claude: structured JSON answers, token usage and cost for every call.

A failed call is returned as an error (and logged), never swallowed.
"""
import base64
import json
from pathlib import Path

import anthropic

from .config import PRICES
from .db import now

_client = None


def client():
    global _client
    if _client is None:
        # The SDK retries rate limits (429) and server errors with backoff.
        # Short connect/read timeouts: on a flaky network a hung connection is retried quickly
        # instead of blocking a worker for minutes.
        _client = anthropic.Anthropic(max_retries=8, timeout=anthropic.Timeout(60.0, connect=10.0))
    return _client


def cost_usd(model, usage) -> float:
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (usage.input_tokens * price_in + cache_write * price_in * 1.25 + cache_read * price_in * 0.1
            + usage.output_tokens * price_out) / 1e6


def image_block(path: str) -> dict:
    data = Path(path).read_bytes()
    media = "image/png" if data.startswith(b"\x89PNG") else "image/gif" if data.startswith(b"GIF8") else "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.standard_b64encode(data).decode()}}


def call_json(step, ref, model, system, content, schema, max_tokens=3000, effort="low", timeout=None,
              fallback_model=None):
    """Returns (parsed_dict_or_None, call_record). call_record goes into the llm_calls table.

    If the model refuses and a fallback_model is given, the same request is tried once on it;
    the record then names the model that produced the answer (or the final error)."""
    ans, rec = _call_json(step, ref, model, system, content, schema, max_tokens, effort, timeout)
    if ans is None and fallback_model and rec["error"] == "model refused":
        ans, rec2 = _call_json(step, ref, fallback_model, system, content, schema, max_tokens, effort, timeout)
        rec2["cost_usd"] += rec["cost_usd"]
        rec2["error"] = rec2["error"] or None
        if ans is not None:
            rec2["error"] = f"{model} refused; answered by {fallback_model}"
        rec = rec2
    return ans, rec


def _call_json(step, ref, model, system, content, schema, max_tokens, effort, timeout):
    record = dict(step=step, ref=ref, model=model, input_tokens=0, output_tokens=0, cache_read=0,
                  cache_write=0, cost_usd=0.0, ok=0, error=None, at=now())
    try:
        api = client().with_options(timeout=timeout) if timeout else client()
        resp = api.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": schema}, "effort": effort},
        )
        u = resp.usage
        record.update(input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                      cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
                      cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
                      cost_usd=cost_usd(model, u))
        if resp.stop_reason == "refusal":
            record["error"] = "model refused"
            return None, record
        if resp.stop_reason == "max_tokens":
            record["error"] = "answer cut off (max_tokens)"
            return None, record
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            record["error"] = "no text block in answer"
            return None, record
        record["ok"] = 1
        return json.loads(text), record
    except anthropic.APIStatusError as e:
        record["error"] = f"API {e.status_code}: {str(e.message)[:200]}"
    except anthropic.APIConnectionError as e:
        record["error"] = f"connection: {str(e)[:200]}"
    except json.JSONDecodeError as e:
        record["error"] = f"invalid JSON: {e}"
    return None, record


def save_call(con, rec):
    con.execute(
        "INSERT INTO llm_calls(step, ref, model, input_tokens, output_tokens, cache_read, cache_write, cost_usd, ok, error, at)"
        " VALUES (:step,:ref,:model,:input_tokens,:output_tokens,:cache_read,:cache_write,:cost_usd,:ok,:error,:at)", rec)
