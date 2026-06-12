"""Python client for LLM Gateway V8. Adds agent/session tagging, a
batch endpoint, and exposes the gateway's `retries` count in the response.

V8 behaviour summary (caller-visible):

  - Pass `agent="planner"` (or any skill name) on a chat call to surface
    the calling skill in the gateway's cost-by-agent ledger and to apply
    `agent_routing.yaml`'s preferred provider when the caller has not
    pinned one explicitly. The pin is a preference; failover still
    happens if the preferred provider is unavailable.
  - Pass `session="<sid>"` to bucket every call from one flow-run
    together so `/v1/cost/by_agent?session=<sid>` can scope its rollup.
  - Call `.chat_batch([req1, req2, ...])` to fire N requests through one
    HTTP round-trip; the gateway dispatches them with bounded
    parallelism so the rate-limit ladder is enforced centrally.
"""
import os
import random
import time
import httpx
from typing import Any, Optional

DEFAULT_URL = os.getenv("LLM_GATEWAY_V9_URL", "http://localhost:8109")

# Transient gateway responses worth retrying. A 503 here means the gateway's
# whole provider ring was momentarily in cooldown (common under a parallel
# fan-out); it clears within seconds. Retrying with backoff keeps the calling
# node alive instead of the orchestrator skipping it as a hard failure.
_RETRY_STATUS = frozenset({502, 503, 504})
_MAX_ATTEMPTS = 4
_BACKOFF_BASE_S = 1.5
_BACKOFF_CAP_S = 12.0


def _retry_sleep(attempt: int) -> None:
    """Exponential backoff with jitter. Sync (callers run this in a worker
    thread via asyncio.to_thread, so time.sleep does not block the loop)."""
    delay = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * (2 ** attempt)) + random.uniform(0, 0.75)
    time.sleep(delay)


def _post_with_retry(url: str, json: dict, timeout: float) -> httpx.Response:
    """POST that retries on transient 5xx / transport errors. 4xx is returned
    as-is for the caller to raise_for_status() on. On the final attempt the
    last response (or transport error) is surfaced unchanged."""
    last_exc: Exception | None = None
    r: httpx.Response | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            r = httpx.post(url, json=json, timeout=timeout)
            if r.status_code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS - 1:
                _retry_sleep(attempt)
                continue
            return r
        except httpx.TransportError as e:  # timeouts, connection resets
            last_exc = e
            if attempt < _MAX_ATTEMPTS - 1:
                _retry_sleep(attempt)
                continue
            raise
    if r is not None:
        return r
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("unreachable: retry loop exited without result")


class LLM:
    def __init__(self, base_url: str = DEFAULT_URL, timeout: float = 600):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def chat(self, prompt: str = None, *,
             messages: Optional[list] = None,
             system: Any = None,
             provider: str = None, model: str = None,
             max_tokens: int = 2048, temperature: float = 0.7,
             tools: Optional[list] = None,
             tool_choice: Any = None,
             cache_system: Optional[bool] = None,
             reasoning: Optional[str] = None,
             response_format: Any = None,
             auto_route: Optional[str] = None,
             agent: Optional[str] = None,
             session: Optional[str] = None) -> dict:
        body = {
            "prompt": prompt, "messages": messages, "system": system,
            "provider": provider, "model": model,
            "max_tokens": max_tokens, "temperature": temperature, "stream": False,
            "tools": tools, "tool_choice": tool_choice,
            "cache_system": cache_system, "reasoning": reasoning,
            "response_format": response_format,
            "auto_route": auto_route,
            "agent": agent, "session": session,
        }
        body = {k: v for k, v in body.items() if v is not None}
        r = _post_with_retry(f"{self.base_url}/v1/chat", json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def chat_batch(self, calls: list[dict], max_concurrency: int = 4) -> list[dict]:
        """Submit N chat requests to the gateway in a single round-trip.
        Each entry in `calls` is a dict matching ChatRequest. Returns the
        list of responses in input order; failed calls are returned as
        `{"error": ..., "status_code": ...}` rather than raising."""
        body = {"calls": calls, "max_concurrency": max_concurrency}
        r = httpx.post(f"{self.base_url}/v1/chat/batch", json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("results", [])

    def capabilities(self):
        return httpx.get(f"{self.base_url}/v1/capabilities", timeout=30).json()

    def cost_by_agent(self, session: Optional[str] = None) -> dict:
        params = {"session": session} if session else {}
        r = httpx.get(f"{self.base_url}/v1/cost/by_agent", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def embed(self, text: str,
              task_type: str = "retrieval_document",
              provider: Optional[str] = None) -> dict:
        """Returns {provider, model, embedding, dim, latency_ms, attempted}."""
        body = {"text": text, "task_type": task_type}
        if provider:
            body["provider"] = provider
        r = httpx.post(f"{self.base_url}/v1/embed", json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()


def ask(prompt: str, provider: str = None, **kw) -> str:
    return LLM().chat(prompt, provider=provider, **kw)["text"]


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else None
    print(ask("Say hello in one short line.", provider=p))
