"""Groq chat-completions client — server-side only, never raises.

Design notes
------------
* No new dependency. Groq exposes an OpenAI-compatible REST endpoint, so this
  uses `requests`, which the project already ships. Swapping provider is a
  base-URL and model change, not a rewrite.
* The API key is read from the environment (`GROQ_API_KEY`), optionally loaded
  from a local `.env` that is git-ignored. It is never logged, never returned
  in a response, and never reaches the browser: every call happens here, in the
  Python process.
* Structured output uses Groq's `json_schema` response format with
  `strict: true` where the model supports constrained decoding, falling back to
  `json_object` mode. Either way the caller re-validates the parsed result.
* Every failure path returns an `LLMResult` with `ok=False` and a short reason.
  Nothing in this module can raise into the pipeline, because the LLM is an
  enhancement layer and must never be able to take the platform down.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

BASE_URL = os.environ.get("ASTRA_LLM_BASE_URL",
                          "https://api.groq.com/openai/v1/chat/completions")
#: gpt-oss-20b supports strict json_schema (constrained decoding) and is the
#: fastest production model on Groq, which matters for a live demo.
DEFAULT_MODEL = os.environ.get("ASTRA_GROQ_MODEL", "openai/gpt-oss-20b")
#: models known to honour `strict: true` constrained decoding
STRICT_SCHEMA_MODELS = ("openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen")
#: Reasoning models think before they write, and the thinking counts against
#: max_tokens. At the provider's default effort gpt-oss can spend the whole
#: budget thinking and return empty fields, or nothing (Groq then answers 400
#: json_validate_failed). The task is explaining supplied evidence, so low is enough.
REASONING_MODELS = ("gpt-oss",)
REASONING_EFFORT = os.environ.get("ASTRA_LLM_REASONING_EFFORT", "low")

TIMEOUT = float(os.environ.get("ASTRA_LLM_TIMEOUT", "20"))
MAX_RETRIES = int(os.environ.get("ASTRA_LLM_RETRIES", "1"))
_ENV_LOADED = False


def _load_dotenv() -> None:
    """Minimal .env reader so no extra dependency is needed.

    Existing environment variables always win, so a real deployment can set
    GROQ_API_KEY properly without the file being consulted at all.
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


def api_key() -> str | None:
    _load_dotenv()
    key = os.environ.get("GROQ_API_KEY", "").strip()
    return key or None


def available() -> bool:
    """Whether an LLM call could be attempted at all."""
    return api_key() is not None


def provider_status() -> dict:
    """Non-sensitive status for the UI and the API. Never exposes the key."""
    key = api_key()
    return {
        "provider": "groq",
        "model": DEFAULT_MODEL,
        "configured": key is not None,
        # a coarse fingerprint only, so an operator can confirm WHICH key is
        # loaded without the value ever being displayed or logged
        "key_hint": (f"…{key[-4:]}" if key and len(key) >= 4 else None),
        "timeout_seconds": TIMEOUT,
    }


@dataclass
class LLMResult:
    ok: bool
    data: dict[str, Any] | None = None
    reason: str = ""
    model: str = ""
    latency_ms: int = 0
    raw_text: str = ""
    usage: dict = field(default_factory=dict)


def _post(payload: dict, key: str) -> requests.Response:
    return requests.post(
        BASE_URL, json=payload, timeout=TIMEOUT,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})


def complete_json(system: str, user: str, schema: dict,
                  schema_name: str = "response",
                  model: str | None = None,
                  temperature: float = 0.2,
                  max_tokens: int = 1400) -> LLMResult:
    """Request a JSON object matching `schema`. Never raises.

    Returns ok=False with a short machine-readable reason on any failure:
    no_api_key, timeout, rate_limited, http_<code>, invalid_json, network.
    """
    key = api_key()
    if not key:
        return LLMResult(False, reason="no_api_key")

    model = model or DEFAULT_MODEL
    strict = any(m in model for m in STRICT_SCHEMA_MODELS)
    base = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if any(m in model for m in REASONING_MODELS) and REASONING_EFFORT:
        base["reasoning_effort"] = REASONING_EFFORT
    attempts = [
        dict(base, response_format={
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": strict,
                            "schema": schema}}),
        # some models/endpoints reject json_schema; plain JSON mode still works
        # and the caller validates the parsed object regardless
        dict(base, response_format={"type": "json_object"}),
    ]

    last_reason = "unknown"
    started = time.monotonic()
    for payload in attempts:
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = _post(payload, key)
            except requests.Timeout:
                last_reason = "timeout"
                break
            except requests.RequestException:
                last_reason = "network"
                break

            if resp.status_code == 200:
                try:
                    body = resp.json()
                    text = body["choices"][0]["message"]["content"]
                    data = json.loads(text)
                except (ValueError, KeyError, IndexError, TypeError):
                    last_reason = "invalid_json"
                    break
                if not isinstance(data, dict):
                    last_reason = "invalid_json"
                    break
                return LLMResult(
                    True, data=data, model=model, raw_text=text,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    usage=body.get("usage", {}) or {})

            if resp.status_code == 429:
                last_reason = "rate_limited"
                retry_after = resp.headers.get("retry-after")
                # one short, bounded wait; a demo must not stall on a retry
                if attempt < MAX_RETRIES and retry_after:
                    try:
                        time.sleep(min(float(retry_after), 3.0))
                        continue
                    except ValueError:
                        pass
                break
            if resp.status_code in (400, 404, 422):
                # likely an unsupported response_format for this model:
                # fall through to the next, simpler attempt
                last_reason = f"http_{resp.status_code}"
                break
            if resp.status_code in (401, 403):
                return LLMResult(False, reason="unauthorized", model=model)
            last_reason = f"http_{resp.status_code}"
            break

    return LLMResult(False, reason=last_reason, model=model,
                     latency_ms=int((time.monotonic() - started) * 1000))
