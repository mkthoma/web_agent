#!/usr/bin/env python3
"""Compare NVIDIA NIM models for Web Agent workloads vs Gemini baseline.

Runs three task types that mirror gateway agent roles:
  1. planner_json  — strict JSON plan with multiple nodes
  2. distiller_json — structured field extraction from pricing text
  3. burst_distiller — 8 parallel distiller calls (fan-out stress)

Usage (from repo root or llm_gatewayV9):
  uv run python llm_gatewayV9/scripts/benchmark_nvidia_models.py
  uv run python llm_gatewayV9/scripts/benchmark_nvidia_models.py --models mistralai/mistral-small-4-119b-2603
  uv run python llm_gatewayV9/scripts/benchmark_nvidia_models.py --skip-burst

Requires NVIDIA_API_KEY and GEMINI_API_KEY in Session 9 v2/.env
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"

DEFAULT_MODELS = [
    "mistralai/mistral-medium-3.5-128b",
    "deepseek-ai/deepseek-v4-flash",
    "minimaxai/minimax-m2.7",
    "mistralai/mistral-small-4-119b-2603",
    "qwen/qwen3.5-122b-a10b",
]

PLANNER_PROMPT = """You are the Planner. Output JSON only, no markdown fences.
{
  "rationale": "<one sentence>",
  "nodes": [
    {"skill": "browser", "inputs": [], "metadata": {"label": "b1", "url": "https://cursor.com/pricing", "goal": "read free and paid plans"}},
    {"skill": "distiller", "inputs": ["n:b1"], "metadata": {"label": "d1"}},
    {"skill": "formatter", "inputs": ["USER_QUERY", "n:d1"], "metadata": {"label": "out"}}
  ]
}
USER_QUERY: Compare Cursor and Copilot pricing. Emit exactly the three nodes above (browser, distiller, formatter). JSON only."""

DISTILLER_PROMPT = """You are the Distiller. Output JSON only, no markdown fences.
{
  "fields": {"tool": "...", "free_plan": "...", "paid_price": "...", "features": "..."},
  "rationale": "..."
}
INPUTS:
Cursor Pro $20/mo. Hobby tier free with limited Agent requests.
Extract tool, free_plan, paid_price, features (up to 3 bullets). JSON only."""

BURST_PROMPT = DISTILLER_PROMPT  # same shape, repeated under load


def parse_json(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if "\n" in t:
            t = t.split("\n", 1)[1]
        if t.endswith("```"):
            t = t[:-3]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(t[start : end + 1])
            except json.JSONDecodeError:
                pass
    return {}


def score_planner(parsed: dict) -> bool:
    nodes = parsed.get("nodes") or []
    if len(nodes) < 3:
        return False
    skills = {n.get("skill") for n in nodes if isinstance(n, dict)}
    return "formatter" in skills and ("browser" in skills or "researcher" in skills)


def score_distiller(parsed: dict) -> bool:
    fields = parsed.get("fields") or {}
    return bool(fields.get("tool") or fields.get("paid_price"))


@dataclass
class TaskResult:
    ok: bool
    latency_ms: float
    error: str = ""
    task_pass: bool = False


@dataclass
class ModelReport:
    model: str
    provider: str
    available: bool = True
    planner: list[TaskResult] = field(default_factory=list)
    distiller: list[TaskResult] = field(default_factory=list)
    burst_ok: int = 0
    burst_total: int = 0
    notes: str = ""

    @property
    def planner_pass_rate(self) -> float:
        if not self.planner:
            return 0.0
        return sum(1 for r in self.planner if r.task_pass) / len(self.planner)

    @property
    def distiller_pass_rate(self) -> float:
        if not self.distiller:
            return 0.0
        return sum(1 for r in self.distiller if r.task_pass) / len(self.distiller)

    @property
    def median_planner_ms(self) -> float | None:
        ok = [r.latency_ms for r in self.planner if r.ok]
        return statistics.median(ok) if ok else None

    @property
    def median_distiller_ms(self) -> float | None:
        ok = [r.latency_ms for r in self.distiller if r.ok]
        return statistics.median(ok) if ok else None


async def nvidia_chat(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    prompt: str,
    *,
    max_tokens: int = 1200,
    temperature: float = 0.2,
    timeout: float = 300.0,
) -> tuple[str, float, str | None]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    t0 = time.perf_counter()
    try:
        r = await client.post(
            NVIDIA_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
    except httpx.TimeoutException:
        elapsed = (time.perf_counter() - t0) * 1000
        return "", elapsed, f"timeout after {timeout:.0f}s"
    elapsed = (time.perf_counter() - t0) * 1000
    if r.status_code != 200:
        return "", elapsed, f"HTTP {r.status_code}: {r.text[:200]}"
    data = r.json()
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return text, elapsed, None


async def gemini_chat(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    prompt: str,
    *,
    max_tokens: int = 1200,
    temperature: float = 0.2,
) -> tuple[str, float, str | None]:
    url = f"{GEMINI_URL}/{model}:generateContent?key={api_key}"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
    }
    t0 = time.perf_counter()
    r = await client.post(url, json=body)
    elapsed = (time.perf_counter() - t0) * 1000
    if r.status_code != 200:
        return "", elapsed, f"HTTP {r.status_code}: {r.text[:200]}"
    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        return "", elapsed, "no candidates"
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts)
    return text, elapsed, None


async def run_task(
    chat_fn,
    prompt: str,
    scorer,
    repeats: int,
) -> list[TaskResult]:
    out: list[TaskResult] = []
    for _ in range(repeats):
        text, ms, err = await chat_fn(prompt)
        if err:
            out.append(TaskResult(ok=False, latency_ms=ms, error=err))
            continue
        parsed = parse_json(text)
        out.append(
            TaskResult(
                ok=bool(parsed),
                latency_ms=ms,
                task_pass=scorer(parsed) if parsed else False,
            )
        )
        await asyncio.sleep(0.5)  # gentle spacing between repeats
    return out


async def benchmark_model(
    client: httpx.AsyncClient,
    model: str,
    *,
    nvidia_key: str,
    repeats: int,
    burst_n: int,
) -> ModelReport:
    report = ModelReport(model=model, provider="nvidia")

    async def chat(p: str):
        return await nvidia_chat(client, nvidia_key, model, p)

    # Probe availability
    _, _, err = await chat("Reply with exactly: OK")
    if err and ("404" in err or "not found" in err.lower() or "invalid model" in err.lower()):
        report.available = False
        report.notes = err
        return report
    if err and "timeout" in err.lower():
        report.available = False
        report.notes = err
        return report

    report.planner = await run_task(chat, PLANNER_PROMPT, score_planner, repeats)
    report.distiller = await run_task(chat, DISTILLER_PROMPT, score_distiller, repeats)

    if burst_n > 0:
        async def one_burst():
            text, ms, err = await chat(BURST_PROMPT)
            if err:
                return False
            return score_distiller(parse_json(text))

        results = await asyncio.gather(*[one_burst() for _ in range(burst_n)])
        report.burst_total = burst_n
        report.burst_ok = sum(1 for x in results if x)

    return report


async def benchmark_gemini(
    client: httpx.AsyncClient,
    model: str,
    api_key: str,
    repeats: int,
) -> ModelReport:
    report = ModelReport(model=model, provider="gemini")

    async def chat(p: str):
        return await gemini_chat(client, api_key, model, p)

    report.planner = await run_task(chat, PLANNER_PROMPT, score_planner, repeats)
    report.distiller = await run_task(chat, DISTILLER_PROMPT, score_distiller, repeats)
    return report


def composite_score(r: ModelReport) -> float:
    """Higher is better: quality first, then speed, then burst."""
    if not r.available:
        return -1.0
    quality = (r.planner_pass_rate * 0.45 + r.distiller_pass_rate * 0.35)
    burst = (r.burst_ok / r.burst_total) * 0.2 if r.burst_total else 0.1
    speed_ms = r.median_distiller_ms or r.median_planner_ms or 99999
    speed_bonus = max(0, 1.0 - speed_ms / 15000) * 0.15
    return quality + burst + speed_bonus


def print_report(reports: list[ModelReport]) -> None:
    print("\n" + "=" * 88)
    print(f"{'Model':<42} {'Plan%':>6} {'Dist%':>6} {'Plan ms':>9} {'Dist ms':>9} {'Burst':>8} {'Score':>6}")
    print("-" * 88)
    ranked = sorted(reports, key=composite_score, reverse=True)
    for r in ranked:
        if not r.available:
            print(f"{r.model:<42} {'N/A':>6} {'N/A':>6} {'N/A':>9} {'N/A':>9} {'N/A':>8} {'N/A':>6}  ({r.notes[:40]})")
            continue
        burst = f"{r.burst_ok}/{r.burst_total}" if r.burst_total else "-"
        print(
            f"{r.model:<42} "
            f"{100*r.planner_pass_rate:5.0f}% "
            f"{100*r.distiller_pass_rate:5.0f}% "
            f"{r.median_planner_ms or 0:8.0f} "
            f"{r.median_distiller_ms or 0:8.0f} "
            f"{burst:>8} "
            f"{composite_score(r):5.2f}"
        )
    print("=" * 88)
    best = next((r for r in ranked if r.available), None)
    if best:
        print(f"\nRecommended NVIDIA_MODEL: {best.model}")
        print(
            f"  planner pass {100*best.planner_pass_rate:.0f}%, "
            f"distiller pass {100*best.distiller_pass_rate:.0f}%, "
            f"median distiller {best.median_distiller_ms:.0f}ms"
        )
    gemini = next((r for r in reports if r.provider == "gemini"), None)
    if gemini and gemini.available:
        print(
            f"\nGemini baseline ({gemini.model}): "
            f"planner {100*gemini.planner_pass_rate:.0f}%, "
            f"distiller {100*gemini.distiller_pass_rate:.0f}%, "
            f"median distiller {gemini.median_distiller_ms or 0:.0f}ms "
            f"(gateway RPM cap ~15 vs NVIDIA ~40)"
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="*", default=DEFAULT_MODELS)
    parser.add_argument("--repeats", type=int, default=2, help="runs per task type")
    parser.add_argument("--burst", type=int, default=8, help="parallel distiller calls (0 to skip)")
    parser.add_argument("--skip-gemini", action="store_true")
    parser.add_argument("--skip-burst", action="store_true")
    args = parser.parse_args()

    nvidia_key = os.getenv("NVIDIA_API_KEY", "")
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

    if not nvidia_key:
        print("NVIDIA_API_KEY missing in .env", file=sys.stderr)
        return 1

    burst_n = 0 if args.skip_burst else args.burst
    reports: list[ModelReport] = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
        for model in args.models:
            print(f"\n[benchmark] {model} ...", flush=True)
            reports.append(
                await benchmark_model(
                    client, model, nvidia_key=nvidia_key, repeats=args.repeats, burst_n=burst_n
                )
            )
            await asyncio.sleep(1.0)

        if not args.skip_gemini and gemini_key:
            print(f"\n[benchmark] gemini baseline {gemini_model} ...", flush=True)
            reports.append(
                await benchmark_gemini(client, gemini_model, gemini_key, args.repeats)
            )

    print_report(reports)

    out_path = ROOT / "llm_gatewayV9" / "scripts" / "benchmark_nvidia_results.json"
    payload = [
        {
            "model": r.model,
            "provider": r.provider,
            "available": r.available,
            "planner_pass_rate": r.planner_pass_rate,
            "distiller_pass_rate": r.distiller_pass_rate,
            "median_planner_ms": r.median_planner_ms,
            "median_distiller_ms": r.median_distiller_ms,
            "burst_ok": r.burst_ok,
            "burst_total": r.burst_total,
            "composite_score": composite_score(r),
            "notes": r.notes,
        }
        for r in reports
    ]
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
