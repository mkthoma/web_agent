"""Agent routing preference + failover candidate ordering."""
from types import SimpleNamespace

import main as gw


class _FakeRouter:
    def __init__(self, providers):
        self.providers = providers
        self.order = list(providers.keys())


def test_failover_prefers_agent_pin_then_ring():
    router = _FakeRouter({"gemini": object(), "nvidia": object(), "groq": object()})
    cands = gw._failover_candidates(router, preferred="nvidia")
    assert cands[0] == "nvidia"
    assert set(cands) == {"gemini", "nvidia", "groq"}
    assert cands.count("nvidia") == 1


def test_failover_tier_intersects_wired_providers():
    router = _FakeRouter({"groq": object(), "gemini": object()})
    cands = gw._failover_candidates(router, preferred="gemini", tier="LARGE")
    assert cands[0] == "gemini"
    assert cands == ["gemini", "groq"]
