"""Framework-free client for llm_gatewayV9.

Plain httpx — no LangChain, no provider SDKs. The shipped Browser skill
talks to the gateway over HTTP, the same way every other S-session skill
does. Provider rotation, retries, agent tagging are the gateway's job.

Two methods: `vision()` hits /v1/vision for Layer-3 set-of-marks calls,
`chat()` hits /v1/chat for Layer-2b a11y-text calls (no image, cheaper,
doesn't require a vision-capable provider). `cost_by_agent()` queries the
gateway's V8 ledger so tests can pull real numbers.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Optional

import httpx

# Transient gateway responses worth retrying. A 503 from the gateway means
# "every provider is momentarily in cooldown" under a parallel fan-out — it
# clears within seconds once cooldowns lapse, so retrying with backoff is the
# right move rather than letting the orchestrator skip the node permanently.
_RETRY_STATUS = frozenset({502, 503, 504})
_MAX_ATTEMPTS = 4
_BACKOFF_BASE_S = 1.5
_BACKOFF_CAP_S = 12.0


async def _retry_sleep(attempt: int) -> None:
    """Exponential backoff with jitter: ~1.5s, 3s, 6s (+ up to 0.75s jitter)."""
    delay = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * (2 ** attempt)) + random.uniform(0, 0.75)
    await asyncio.sleep(delay)


@dataclass
class GatewayResult:
    """Normalised reply from either /v1/vision or /v1/chat."""
    parsed: dict | None
    text: str
    provider: str
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int


# Back-compat alias — the early SoM driver imports `VisionResult`.
VisionResult = GatewayResult


class V9Client:
    """One client, two methods: vision() and chat(). Both speak to V9."""
    def __init__(
        self,
        base_url: str = "http://localhost:8109",
        agent: str = "s9_browser",
        timeout: float = 120.0,
        session: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.agent = agent
        self.timeout = timeout
        # Default session tag for ledger attribution. Per-call overrides win.
        self.session = session

    @staticmethod
    def _normalise(d: dict) -> GatewayResult:
        return GatewayResult(
            parsed=d.get("parsed"),
            text=d.get("text") or "",
            provider=d.get("provider", ""),
            model=d.get("model", ""),
            latency_ms=int(d.get("latency_ms") or 0),
            input_tokens=int(d.get("input_tokens") or 0),
            output_tokens=int(d.get("output_tokens") or 0),
        )

    async def _post_json(self, path: str, body: dict) -> dict:
        """POST with bounded retry on transient gateway errors.

        Retries on 5xx (gateway ring momentarily exhausted) and transport
        errors (ReadTimeout / ConnectError — e.g. the gateway is mid-restart).
        4xx is surfaced immediately: those are real request bugs, not load.
        On the final attempt the original httpx error is re-raised so the
        orchestrator's failure classifier still sees an `HTTPStatusError`.
        """
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as c:
                    r = await c.post(url, json=body)
                if r.status_code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS - 1:
                    await _retry_sleep(attempt)
                    continue
                r.raise_for_status()
                return r.json()
            except httpx.TransportError as e:  # timeouts, connection resets
                last_exc = e
                if attempt < _MAX_ATTEMPTS - 1:
                    await _retry_sleep(attempt)
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("unreachable: retry loop exited without result")

    async def vision(
        self,
        image_data_url: str,
        prompt: str,
        *,
        schema: Optional[dict] = None,
        schema_name: str = "out",
        system: Optional[str] = None,
        max_tokens: int = 1024,
        session: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> GatewayResult:
        body: dict[str, Any] = {
            "image": image_data_url,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "agent": self.agent,
        }
        if schema:        body["schema"] = schema
        if schema:        body["schema_name"] = schema_name
        if system:        body["system"] = system
        s = session or self.session
        if s:             body["session"] = s
        if model:         body["model"] = model
        if provider:      body["provider"] = provider

        return self._normalise(await self._post_json("/v1/vision", body))

    async def chat(
        self,
        prompt: str,
        *,
        schema: Optional[dict] = None,
        schema_name: str = "out",
        system: Optional[str] = None,
        max_tokens: int = 1024,
        session: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> GatewayResult:
        """Plain text-only call. Used by the Layer-2b a11y driver: legend +
        goal in, action JSON out. Skipping the image cuts ~1K input tokens
        per turn vs vision()."""
        body: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "agent": self.agent,
        }
        if schema:
            body["response_format"] = {
                "type": "json_schema", "schema": schema,
                "name": schema_name, "strict": True,
            }
        if system:    body["system"] = system
        s = session or self.session
        if s:         body["session"] = s
        if model:     body["model"] = model
        if provider:  body["provider"] = provider

        return self._normalise(await self._post_json("/v1/chat", body))

    async def cost_by_agent(self, agent: Optional[str] = None,
                            session: Optional[str] = None) -> dict:
        """Pull the V9 ledger for this agent/session — tests use it to
        report real numbers rather than wall-clock estimates."""
        params: dict[str, Any] = {}
        if agent:   params["agent"] = agent
        if session: params["session"] = session
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.get(f"{self.base_url}/v1/cost/by_agent", params=params)
            r.raise_for_status()
            return r.json()


# Back-compat alias.
V9VisionClient = V9Client
