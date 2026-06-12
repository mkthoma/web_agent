"""Bridge to llm_gatewayV9.

V9 adds (1) `/v1/vision` for single-image vision calls used by the Browser
skill's Layer-3 driver; (2) per-agent USD pricing on `/v1/cost/by_agent`.
V8's `agent` tagging, `/v1/chat/batch`, and retry-on-5xx carry forward.

Auto-starts the gateway on port 8109 if it is not already up, then
re-exports the V9 `LLM` client and a module-level `embed()` helper.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import httpx

def _find_gateway_dir() -> Path:
    """Locate llm_gatewayV9/ by walking up from this file."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "llm_gatewayV9"
        if (candidate / "client.py").is_file():
            return candidate
    raise RuntimeError(
        "llm_gatewayV9/client.py not found in any parent of "
        f"{Path(__file__).resolve()}"
    )


GATEWAY_V9_DIR = _find_gateway_dir()
GATEWAY_URL = "http://localhost:8109"


def _is_up() -> bool:
    try:
        httpx.get(f"{GATEWAY_URL}/v1/routers", timeout=2.0)
        return True
    except Exception:
        return False


def ensure_gateway() -> None:
    """Start V9 if it is not already running. Idempotent."""
    if _is_up():
        return
    if not GATEWAY_V9_DIR.exists():
        raise RuntimeError(
            f"Gateway V9 directory not found at {GATEWAY_V9_DIR}. "
            "Build llm_gatewayV9 before running Web Agent code."
        )
    print(f"[webagent] launching llm_gatewayV9 from {GATEWAY_V9_DIR}")
    subprocess.Popen(
        ["uv", "run", "main.py"],
        cwd=str(GATEWAY_V9_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(45):
        time.sleep(1)
        if _is_up():
            print(f"[webagent] gateway up on {GATEWAY_URL}")
            return
    raise RuntimeError(f"Gateway V9 failed to start within 45s. Check {GATEWAY_V9_DIR}")


# Load V9's client.py without polluting sys.path. The gateway dir has its
# own `schemas.py`, which would shadow ours if we put it on the path.
import importlib.util as _importlib_util

_client_path = GATEWAY_V9_DIR / "client.py"
if _client_path.exists():
    _spec = _importlib_util.spec_from_file_location("llm_gatewayV9_client", _client_path)
    _mod = _importlib_util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    LLM = _mod.LLM
else:
    LLM = None  # populated once V9 is built; importers should ensure_gateway() first


def embed(text: str, task_type: str = "retrieval_document") -> dict:
    """Compute an embedding for `text` via the gateway's embed endpoint."""
    ensure_gateway()
    if LLM is None:
        raise RuntimeError(
            "Gateway V9 client unavailable. Confirm llm_gatewayV9/client.py exists."
        )
    return LLM().embed(text, task_type=task_type)


__all__ = ["ensure_gateway", "LLM", "GATEWAY_URL", "GATEWAY_V9_DIR", "embed"]
