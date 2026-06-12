"""Read-only tests for comparison_report.py — no network, synthetic session."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from comparison_report import build_report, generate_report, render_html
from persistence import SessionStore
from schemas import AgentResult, BrowserOutput, NodeState


@pytest.fixture
def synthetic_session(tmp_path, monkeypatch):
    """Minimal session with all 8 report elements."""
    monkeypatch.setattr("comparison_report.SESSIONS_ROOT", tmp_path / "sessions")
    monkeypatch.setattr("persistence.SESSIONS_ROOT", tmp_path / "sessions")

    sid = "test-comparison-001"
    store = SessionStore(sid)
    store.write_query("Compare GitHub Copilot and Cursor pricing.")

    import networkx as nx

    graph = nx.DiGraph()
    graph.add_node("n:1", skill="planner", inputs=[], status="complete")
    graph.add_node("n:2", skill="browser", inputs=[], status="complete", metadata={"label": "b1"})
    graph.add_node("n:3", skill="distiller", inputs=["n:2"], status="complete", metadata={"label": "d1"})
    graph.add_node("n:4", skill="formatter", inputs=["USER_QUERY", "n:3"], status="complete", metadata={"label": "out"})
    graph.add_edge("n:1", "n:2")
    graph.add_edge("n:2", "n:3")
    graph.add_edge("n:3", "n:4")
    store.write_graph(graph)

    browser_out = BrowserOutput(
        url="https://example.com/pricing",
        goal="click the billing toggle and read prices",
        path="a11y",
        turns=2,
        content="Free tier $0. Pro $10/mo.",
        actions=[
            {"turn": 1, "actions": [{"type": "click", "target": "Monthly"}], "outcome": "ok"},
            {"turn": 2, "actions": [{"type": "read", "target": "plan"}], "outcome": "ok"},
        ],
        final_url="https://example.com/pricing",
    )
    store.write_node(
        NodeState(
            node_id="n:1",
            skill="planner",
            status="complete",
            inputs=["USER_QUERY"],
            result=AgentResult(success=True, agent_name="planner", output={"nodes": []}),
        )
    )
    store.write_node(
        NodeState(
            node_id="n:2",
            skill="browser",
            status="complete",
            inputs=[],
            result=AgentResult(
                success=True,
                agent_name="browser",
                output=browser_out.model_dump(),
            ),
        )
    )
    store.write_node(
        NodeState(
            node_id="n:3",
            skill="distiller",
            status="complete",
            inputs=["n:2"],
            result=AgentResult(
                success=True,
                agent_name="distiller",
                output={
                    "fields": {
                        "tool": "GitHub Copilot",
                        "free_plan": "Limited free",
                        "paid_price": "$10/mo",
                        "features": "Completions, chat, agents",
                    },
                    "rationale": "From browser content.",
                },
            ),
        )
    )
    store.write_node(
        NodeState(
            node_id="n:4",
            skill="formatter",
            status="complete",
            inputs=["USER_QUERY", "n:3"],
            result=AgentResult(
                success=True,
                agent_name="formatter",
                output={
                    "final_answer": "| Tool | Free | Paid |\n|---|---|---|\n| Copilot | Yes | $10 |"
                },
            ),
        )
    )

    browser_dir = store.dir / "browser" / "browser_test" / "a11y"
    browser_dir.mkdir(parents=True)
    png = browser_dir / "turn_01_raw.png"
    png.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
        b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    return sid, store


def test_all_eight_elements_extract(synthetic_session, monkeypatch):
    sid, _ = synthetic_session
    monkeypatch.setattr("comparison_report.fetch_cost_by_agent", lambda s: {"browser": []})

    report = build_report(sid)

    assert report["1_user_goal"]
    assert "graph TD" in report["2_planner_dag_mermaid"]
    assert report["2_planner_dag_text"]
    assert len(report["3_browser_paths"]) == 1
    assert report["3_browser_paths"][0]["path"] == "a11y"
    assert len(report["4_browser_actions"][0]["actions"]) == 2
    assert len(report["5_page_state_logs"]) >= 1
    assert len(report["6_extracted_data"]) == 1
    assert report["7_final_table"]
    assert report["8_total_browser_turns"] == 2
    assert "8_cost_by_agent" in report


def test_blocked_path_detection(synthetic_session, monkeypatch):
    sid, store = synthetic_session
    monkeypatch.setattr("comparison_report.fetch_cost_by_agent", lambda s: {})

    st = store.read_node("n:2")
    assert st is not None
    st.result = AgentResult(
        success=False,
        agent_name="browser",
        error_code="gateway_blocked",
        error="CAPTCHA wall",
        output={},
    )
    store.write_node(st)

    report = build_report(sid)
    assert report["3_browser_paths"][0]["path"] == "blocked"
    assert report["3_browser_paths"][0]["error_code"] == "gateway_blocked"


def test_self_contained_html_render(synthetic_session, monkeypatch):
    sid, store = synthetic_session
    monkeypatch.setattr("comparison_report.fetch_cost_by_agent", lambda s: {})

    report = build_report(sid)
    html_out = render_html(report, store)

    assert "<!DOCTYPE html>" in html_out
    assert "data:image/png;base64," in html_out
    assert sid in html_out
    assert "Compare GitHub Copilot" in html_out

    generate_report(sid, write_html=True)
    assert (store.dir / "REPORT.html").is_file()
    content = (store.dir / "REPORT.html").read_text(encoding="utf-8")
    assert "data:image/png;base64," in content


def test_fallback_table_without_formatter(synthetic_session, monkeypatch):
    sid, store = synthetic_session
    monkeypatch.setattr("comparison_report.fetch_cost_by_agent", lambda s: {})

    fmt = store.read_node("n:4")
    assert fmt is not None
    fmt.status = "pending"
    fmt.result = None
    store.write_node(fmt)

    report = build_report(sid)
    table = report.get("7_final_table") or ""
    assert "| Tool |" in table or "| GitHub Copilot |" in table
    assert report.get("7_final_table_incomplete") is True
