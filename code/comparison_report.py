"""Read-only replay report for a persisted session.

Consumes SessionStore + on-disk browser artifacts. Never orchestrates.
Prints eight required elements to console and writes REPORT.html.

Usage:
    uv run python comparison_report.py <session_id>
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx
import networkx as nx

from gateway import GATEWAY_URL
from persistence import SESSIONS_ROOT, SessionStore, list_sessions
from schemas import AgentResult, BrowserOutput, NodeState


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_configure_stdio()

# Minimal 1x1 PNG for tests / missing screenshots.
_PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _node_file_id(node_id: str) -> str:
    """n:1 -> n_001 for filesystem lookup."""
    if node_id.startswith("n:"):
        try:
            num = int(node_id.split(":", 1)[1])
            return f"n_{num:03d}"
        except ValueError:
            pass
    return node_id.replace(":", "_")


def _load_browser_output(st: NodeState) -> BrowserOutput | None:
    if not st.result or not st.result.output:
        return None
    try:
        return BrowserOutput.model_validate(st.result.output)
    except Exception:
        return None


_LABEL_TO_TOOL: dict[str, str] = {
    "bCopilot": "GitHub Copilot",
    "bCursor": "Cursor",
    "bClaude": "Claude Code",
    "bWindsurf": "Windsurf",
    "bTabnine": "Tabnine",
    "dCopilot": "GitHub Copilot",
    "dCursor": "Cursor",
    "dClaude": "Claude Code",
    "dWindsurf": "Windsurf",
    "dTabnine": "Tabnine",
}


def _tool_name_from_metadata(metadata: dict[str, Any] | None, fallback: str = "") -> str:
    if not metadata:
        return fallback or "unknown"
    label = str(metadata.get("label") or "")
    if label in _LABEL_TO_TOOL:
        return _LABEL_TO_TOOL[label]
    url = str(metadata.get("url") or "")
    if "copilot" in url.lower():
        return "GitHub Copilot"
    if "cursor.com" in url.lower():
        return "Cursor"
    if "claude" in url.lower() and "anthropic" in url.lower():
        return "Claude Code"
    if "windsurf" in url.lower():
        return "Windsurf"
    if "tabnine" in url.lower():
        return "Tabnine"
    if label.startswith("b") and len(label) > 1:
        return label[1:]
    return fallback or label or "unknown"


def _browser_done_summary(st: NodeState, bo: BrowserOutput | None) -> str:
    if bo and bo.actions:
        for turn in reversed(bo.actions):
            if not isinstance(turn, dict):
                continue
            for act in reversed(turn.get("actions") or []):
                if isinstance(act, dict) and act.get("type") == "done":
                    val = act.get("value")
                    if val:
                        return str(val).strip()
    if bo and bo.content:
        text = bo.content.strip()
        if text:
            return text[:800]
    if st.result and st.result.error:
        return f"(failed: {str(st.result.error)[:160]})"
    if st.status in ("skipped", "failed"):
        return f"({st.status}: no pricing data captured)"
    return "(not found)"


def _features_cell(raw: Any) -> str:
    if isinstance(raw, list):
        return "; ".join(str(x) for x in raw[:3] if str(x).strip())
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:400]
    return "(not found)"


def comparator_matrix_to_markdown(data: dict[str, Any] | None) -> str | None:
    if not data or not isinstance(data, dict):
        return None
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    header = ["Tool", "Free plan", "Cheapest paid", "Headline features"]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in rows:
        if not isinstance(row, dict):
            continue
        tool = str(row.get("tool") or "unknown")
        free = str(row.get("free_plan") or "(not found)")
        paid = str(row.get("paid_price") or "(not found)")
        feats = _features_cell(row.get("features"))
        lines.append(f"| {tool} | {free} | {paid} | {feats} |")
    return "\n".join(lines) if len(lines) > 2 else None


def build_fallback_comparison_table(
    states: list[NodeState],
    graph: nx.DiGraph | None,
) -> str | None:
    """Build a partial markdown table from browser/distiller artifacts."""
    distiller_by_browser: dict[str, dict[str, Any]] = {}
    for st in states:
        if st.skill != "distiller" or not st.result or not st.result.output:
            continue
        out = st.result.output if isinstance(st.result.output, dict) else {}
        fields = out.get("fields") if isinstance(out.get("fields"), dict) else {}
        for inp in st.inputs:
            if inp.startswith("n:"):
                distiller_by_browser[inp] = fields or {}

    browser_states = [st for st in states if st.skill == "browser"]
    if not browser_states:
        return None

    def _sort_key(st: NodeState) -> tuple[int, str]:
        md = {}
        if graph is not None and st.node_id in graph.nodes:
            md = graph.nodes[st.node_id].get("metadata") or {}
        label = str(md.get("label") or st.node_id)
        order = {"bCopilot": 0, "bCursor": 1, "bClaude": 2, "bWindsurf": 3, "bTabnine": 4}
        return (order.get(label, 99), label)

    browser_states.sort(key=_sort_key)

    header = ["Tool", "Free plan", "Cheapest paid", "Headline features", "Status"]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for st in browser_states:
        md = {}
        if graph is not None and st.node_id in graph.nodes:
            md = graph.nodes[st.node_id].get("metadata") or {}
        bo = _load_browser_output(st)
        fields = distiller_by_browser.get(st.node_id, {})
        tool = str(fields.get("tool") or _tool_name_from_metadata(md))
        free = str(fields.get("free_plan") or "(not found)")
        paid = str(fields.get("paid_price") or fields.get("cheapest_paid") or "(not found)")
        feats = _features_cell(fields.get("features"))
        if free == "(not found)" and paid == "(not found)" and feats == "(not found)":
            summary = _browser_done_summary(st, bo)
            if summary and not summary.startswith("(failed"):
                free = summary[:300]
        status = st.status
        if st.result and not st.result.success and status == "complete":
            status = "failed"
        lines.append(f"| {tool} | {free} | {paid} | {feats} | {status} |")

    body = "\n".join(lines)
    if len(lines) <= 2:
        return None
    return (
        "_Partial table — formatter/comparator did not finish; values from "
        "browser/distiller artifacts where available._\n\n" + body
    )


def resolve_final_table(
    states: list[NodeState],
    graph: nx.DiGraph | None,
    *,
    prefer_formatter: bool = True,
) -> str | None:
    formatter_answer: str | None = None
    comparator_data: dict[str, Any] | None = None
    for st in states:
        if st.skill == "formatter" and st.status == "complete" and st.result and st.result.output:
            out = st.result.output
            if isinstance(out, dict):
                fa = out.get("final_answer")
                if isinstance(fa, str) and fa.strip():
                    formatter_answer = fa.strip()
        elif st.skill == "comparator" and st.status == "complete" and st.result and st.result.output:
            if isinstance(st.result.output, dict):
                comparator_data = st.result.output

    if prefer_formatter and formatter_answer:
        return formatter_answer
    cmp_table = comparator_matrix_to_markdown(comparator_data)
    if cmp_table:
        return cmp_table
    if not prefer_formatter and formatter_answer:
        return formatter_answer
    return build_fallback_comparison_table(states, graph)


def _dag_mermaid(graph: nx.DiGraph | None, states: list[NodeState]) -> str:
    lines = ["graph TD"]
    if graph is not None and graph.number_of_nodes() > 0:
        for nid, data in graph.nodes(data=True):
            skill = data.get("skill", "?")
            safe_id = str(nid).replace(":", "_")
            lines.append(f'  {safe_id}["{nid}<br/>{skill}"]')
        for u, v in graph.edges():
            lines.append(f"  {str(u).replace(':', '_')} --> {str(v).replace(':', '_')}")
    else:
        for st in states:
            safe_id = st.node_id.replace(":", "_")
            lines.append(f'  {safe_id}["{st.node_id}<br/>{st.skill}"]')
    return "\n".join(lines)


def _dag_text(graph: nx.DiGraph | None, states: list[NodeState]) -> str:
    rows: list[str] = []
    if graph is not None and graph.number_of_nodes():
        for u, v in graph.edges():
            us = graph.nodes[u].get("skill", "?")
            vs = graph.nodes[v].get("skill", "?")
            rows.append(f"  {u} ({us}) -> {v} ({vs})")
    else:
        for st in states:
            ins = ", ".join(st.inputs) if st.inputs else "(none)"
            rows.append(f"  {st.node_id} skill={st.skill} inputs=[{ins}]")
    return "\n".join(rows) if rows else "  (empty graph)"


def _browser_artifacts(session_dir: Path) -> list[dict[str, Any]]:
    browser_root = session_dir / "browser"
    if not browser_root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for run_dir in sorted(browser_root.glob("browser_*")):
        for layer_dir in sorted(run_dir.iterdir()):
            if not layer_dir.is_dir():
                continue
            for png in sorted(layer_dir.glob("turn_*_raw.png")):
                rel = png.relative_to(session_dir)
                marked = png.with_name(png.name.replace("_raw.png", "_marked.png"))
                legend = png.with_name(png.stem.replace("_raw", "") + "_legend.txt")
                if not legend.exists():
                    alt = png.parent / png.name.replace("_raw.png", "_legend.txt")
                    legend = alt if alt.exists() else legend
                session_dir_str = str(session_dir)

                def _rel(p: Path) -> str | None:
                    try:
                        return str(p.relative_to(session_dir)).replace("\\", "/")
                    except ValueError:
                        return None

                items.append(
                    {
                        "path": str(rel).replace("\\", "/"),
                        "layer": layer_dir.name,
                        "turn": _turn_label(png.name),
                        "raw": png,
                        "marked": marked if marked.exists() else None,
                        "legend": legend if legend.exists() else None,
                        # POSIX rel strings for the live UI (it loads these via
                        # /api/sessions/<sid>/artifacts/<rel>). The Path fields
                        # above stay for render_html's base64 embedding.
                        "raw_rel": _rel(png),
                        "marked_rel": _rel(marked) if marked.exists() else None,
                        "legend_rel": _rel(legend) if legend.exists() else None,
                    }
                )
    return items


def _turn_label(filename: str) -> str:
    """turn_03_raw.png -> 'turn 03'."""
    m = re.match(r"(turn_\d+)", filename)
    return m.group(1).replace("_", " ") if m else filename


def _embed_image(path: Path | None) -> str:
    if path is None or not path.is_file():
        return base64.b64encode(_PLACEHOLDER_PNG).decode("ascii")
    return base64.b64encode(path.read_bytes()).decode("ascii")


def fetch_cost_by_agent(session_id: str) -> dict[str, Any]:
    try:
        r = httpx.get(
            f"{GATEWAY_URL}/v1/cost/by_agent",
            params={"session": session_id},
            timeout=10.0,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}


def build_report(session_id: str) -> dict[str, Any]:
    store = SessionStore(session_id)
    states = store.read_all_nodes()
    graph = store.read_graph()
    query = store.read_query()

    browser_nodes: list[dict[str, Any]] = []
    distiller_data: list[dict[str, Any]] = []
    comparator_data: dict[str, Any] | None = None
    formatter_answer: str | None = None
    total_turns = 0

    for st in states:
        if st.skill == "browser":
            bo = _load_browser_output(st)
            blocked = st.result and st.result.error_code == "gateway_blocked"
            path = bo.path if bo else ("blocked" if blocked else "unknown")
            actions = bo.actions if bo else []
            turns = bo.turns if bo else 0
            total_turns += turns
            browser_nodes.append(
                {
                    "node_id": st.node_id,
                    "path": path,
                    "turns": turns,
                    "actions": actions,
                    "content_preview": (bo.content or "")[:500] if bo else "",
                    "error_code": st.result.error_code if st.result else None,
                    "url": bo.url if bo else "",
                    "final_url": bo.final_url if bo else None,
                }
            )
        elif st.skill == "distiller" and st.result and st.result.output:
            out = st.result.output
            fields = out.get("fields") if isinstance(out, dict) else {}
            distiller_data.append(
                {"node_id": st.node_id, "fields": fields or {}, "raw": out}
            )
        elif st.skill == "comparator" and st.result and st.result.output:
            comparator_data = st.result.output if isinstance(st.result.output, dict) else None
        elif st.skill == "formatter" and st.result and st.result.output:
            out = st.result.output
            if isinstance(out, dict):
                formatter_answer = out.get("final_answer") or json.dumps(out)

    artifacts = _browser_artifacts(store.dir)
    cost = fetch_cost_by_agent(session_id)

    formatter_ran = any(
        st.skill == "formatter" and st.status == "complete" and st.result
        for st in states
    )
    if not formatter_answer:
        formatter_answer = resolve_final_table(states, graph, prefer_formatter=True)

    starts = [st.started_at for st in states if st.started_at]
    ends = [st.completed_at for st in states if st.completed_at]
    wall_clock_s = (max(ends) - min(starts)) if starts and ends else None
    browser_action_count = sum(len(b["actions"]) for b in browser_nodes)

    return {
        "session_id": session_id,
        "1_user_goal": query,
        "2_planner_dag_mermaid": _dag_mermaid(graph, states),
        "2_planner_dag_text": _dag_text(graph, states),
        "3_browser_paths": browser_nodes,
        "4_browser_actions": [
            {"node_id": b["node_id"], "actions": b["actions"]} for b in browser_nodes
        ],
        "5_page_state_logs": artifacts,
        "5_browser_content_fallbacks": [
            {"node_id": b["node_id"], "content": b["content_preview"]}
            for b in browser_nodes
            if b["path"] == "extract" or not artifacts
        ],
        "6_extracted_data": distiller_data,
        "7_comparator_matrix": comparator_data,
        "7_final_table": formatter_answer,
        "7_final_table_incomplete": bool(formatter_answer) and not formatter_ran,
        "8_total_browser_turns": total_turns,
        "8_browser_action_count": browser_action_count,
        "8_node_count": len(states),
        "8_wall_clock_s": wall_clock_s,
        "8_cost_by_agent": cost,
        "nodes": [
            {
                "node_id": st.node_id,
                "skill": st.skill,
                "status": st.status,
                "inputs": st.inputs,
            }
            for st in states
        ],
    }


_REPORT_CSS = """
:root{--bg:#0d1117;--panel:#161b22;--line:#272d36;--ink:#e6edf3;--muted:#8b949e;
--accent:#58a6ff;--extract:#3fb950;--a11y:#58a6ff;--vision:#bc8cff;--blocked:#f85149;}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;padding:2rem;max-width:1100px;margin:auto}
h1{font-size:1.5rem}.sid{color:var(--muted);font-weight:400;font-size:1rem}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:1rem 1.25rem;margin:1rem 0}h2{font-size:1rem;margin:.2rem 0 .8rem;color:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:.86rem}
th,td{border:1px solid var(--line);padding:.4rem .55rem;text-align:left;vertical-align:top}
th{background:#0d1117;color:var(--muted)}.url{word-break:break-all;color:var(--muted)}
pre{white-space:pre-wrap;word-break:break-word;background:#0d1117;border:1px solid var(--line);
border-radius:8px;padding:.7rem;overflow:auto;font-size:.82rem;margin:.3rem 0}
.muted{color:var(--muted)}.warn{color:#f0b429;margin:.5rem 0}.goal{font-size:1.05rem}
.pill{padding:.1rem .55rem;border-radius:999px;font-size:.78rem;color:#0d1117;font-weight:700}
.pill-extract{background:var(--extract)}.pill-a11y{background:var(--a11y)}
.pill-vision{background:var(--vision)}.pill-blocked{background:var(--blocked)}
.pill-unknown{background:var(--muted)}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:1rem}
figure{margin:0;background:#0d1117;border:1px solid var(--line);border-radius:8px;padding:.5rem}
figure img{width:100%;border-radius:4px;border:1px solid var(--line)}
figcaption{color:var(--muted);font-size:.78rem;margin:.4rem 0}
details summary{cursor:pointer;color:var(--accent)}.answer table{font-size:.9rem}
"""


def _pill_class(path: str) -> str:
    return {
        "extract": "pill-extract",
        "a11y": "pill-a11y",
        "vision": "pill-vision",
        "blocked": "pill-blocked",
        "deterministic": "pill-a11y",
    }.get(path, "pill-unknown")


def _pill_html(path: str) -> str:
    cls = _pill_class(str(path))
    label = html.escape(str(path))
    return f'<span class="pill {cls}">{label}</span>'


def _markdown_table_to_html(text: str) -> str | None:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    start = next((i for i, ln in enumerate(lines) if ln.startswith("|")), None)
    if start is None:
        return None
    lines = lines[start:]
    if len(lines) < 2:
        return None
    header = [c.strip() for c in lines[0].strip("|").split("|")]
    body_start = 2 if len(lines) > 1 and re.match(r"^\|[\s\-:|]+\|$", lines[1]) else 1
    rows: list[list[str]] = []
    for ln in lines[body_start:]:
        if not ln.startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) == len(header):
            rows.append(cells)
    if not rows:
        return None
    out = ["<table><tbody>"]
    out.append("<tr>" + "".join(f"<th>{html.escape(c)}</th>" for c in header) + "</tr>")
    for row in rows:
        out.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in row) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _final_table_html(report: dict[str, Any]) -> str:
    answer = report.get("7_final_table") or ""
    parts: list[str] = []
    if report.get("7_final_table_incomplete"):
        parts.append(
            '<p class="warn">Partial run — the formatter did not complete. '
            "This table was rebuilt from comparator/distiller/browser artifacts.</p>"
        )
    if answer.strip().startswith("<"):
        parts.append(f'<div class="answer">{answer}</div>')
        return "".join(parts) or '<p class="muted">(none)</p>'
    table = _markdown_table_to_html(answer)
    if table:
        parts.append(f'<div class="answer">{table}</div>')
        return "".join(parts)
    if report.get("7_comparator_matrix"):
        parts.append(
            "<details><summary>Comparator matrix (JSON)</summary>"
            f"<pre>{html.escape(json.dumps(report['7_comparator_matrix'], indent=2))}</pre></details>"
        )
    if answer:
        parts.append(f"<pre>{html.escape(answer)}</pre>")
    return "".join(parts) or '<p class="muted">(none)</p>'


def _browser_table_html(browser_nodes: list[dict[str, Any]]) -> str:
    if not browser_nodes:
        return '<p class="muted">(no browser nodes)</p>'
    rows = []
    for b in browser_nodes:
        actions_json = html.escape(json.dumps(b.get("actions") or [], indent=2))
        n_actions = len(b.get("actions") or [])
        final_url = html.escape(b.get("final_url") or b.get("url") or "")
        rows.append(
            "<tr>"
            f"<td>{html.escape(b['node_id'])}</td>"
            f"<td>{_pill_html(str(b.get('path', 'unknown')))}</td>"
            f"<td>{b.get('turns', 0)}</td>"
            f'<td class="url">{final_url}</td>'
            f"<td><details><summary>{n_actions} action(s)</summary>"
            f"<pre>{actions_json}</pre></details></td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>node</th><th>path</th><th>turns</th>"
        "<th>final url</th><th>actions</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _gallery_html(report: dict[str, Any]) -> str:
    figures = []
    for art in report.get("5_page_state_logs") or []:
        b64 = _embed_image(art["raw"])
        cap = html.escape(art["path"])
        legend_block = ""
        if art.get("legend") and art["legend"].is_file():
            legend_block = (
                f'<details><summary>legend</summary>'
                f"<pre>{html.escape(art['legend'].read_text(encoding='utf-8', errors='replace')[:4000])}</pre></details>"
            )
        figures.append(
            f'<figure><img src="data:image/png;base64,{b64}" alt="{cap}"/>'
            f"<figcaption>{cap}</figcaption>{legend_block}</figure>"
        )
    for fb in report.get("5_browser_content_fallbacks") or []:
        if fb.get("content"):
            figures.append(
                f"<details><summary>Extract content ({html.escape(fb['node_id'])})</summary>"
                f"<pre>{html.escape(fb['content'])}</pre></details>"
            )
    if not figures:
        return '<p class="muted">(no browser artifacts — extract-only or no browser run)</p>'
    return f'<div class="gallery">{"".join(figures)}</div>'


def _extracted_html(distiller_data: list[dict[str, Any]]) -> str:
    if not distiller_data:
        return '<p class="muted">(none)</p>'
    blocks = []
    for d in distiller_data:
        fields = d.get("fields") or {}
        raw = d.get("raw") if isinstance(d.get("raw"), dict) else {}
        label = fields.get("tool") or fields.get("item") or d.get("node_id", "?")
        conf = raw.get("confidence")
        summary = html.escape(f"{label} (confidence {conf})")
        payload = html.escape(json.dumps(fields or raw, indent=2))
        blocks.append(
            f'<details open><summary>{summary}</summary><pre>{payload}</pre></details>'
        )
    return "".join(blocks)


def _cost_table_html(cost: dict[str, Any]) -> str:
    rows = []
    for agent, entries in sorted(cost.items()):
        if not isinstance(entries, list):
            continue
        for row in entries:
            if not isinstance(row, dict):
                continue
            dollars = row.get("dollars", 0.0)
            try:
                dollars_fmt = f"{float(dollars):.6f}".rstrip("0").rstrip(".")
            except (TypeError, ValueError):
                dollars_fmt = html.escape(str(dollars))
            rows.append(
                "<tr>"
                f"<td>{html.escape(str(agent))}</td>"
                f"<td>{html.escape(str(row.get('provider', '')))}</td>"
                f"<td>{row.get('calls', 0)}</td>"
                f"<td>{row.get('in_tok', 0)}</td>"
                f"<td>{row.get('out_tok', 0)}</td>"
                f"<td>{dollars_fmt}</td>"
                "</tr>"
            )
    if not rows:
        return "<pre>{}</pre>"
    return (
        "<table><thead><tr><th>agent</th><th>provider</th><th>calls</th>"
        "<th>in tok</th><th>out tok</th><th>$</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render_html(report: dict[str, Any], store: SessionStore) -> str:
    sid = html.escape(report["session_id"])
    goal = html.escape(report["1_user_goal"] or "")
    dag_text = html.escape(report["2_planner_dag_text"])
    mermaid = html.escape(report["2_planner_dag_mermaid"])

    wall = report.get("8_wall_clock_s")
    wall_str = f"{wall:.1f}s" if isinstance(wall, (int, float)) else "n/a"
    stats = (
        f"Nodes: <b>{report.get('8_node_count', 0)}</b> · "
        f"Browser turns: <b>{report.get('8_total_browser_turns', 0)}</b> · "
        f"Browser actions: <b>{report.get('8_browser_action_count', 0)}</b> · "
        f"Wall-clock: <b>{wall_str}</b>"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Replay · {sid}</title>
  <style>{_REPORT_CSS}</style>
</head>
<body>
<script type="module">
try{{const m=await import('https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs');
m.default.initialize({{startOnLoad:true,theme:'dark'}});}}catch(e){{}}
</script>
<h1>Replay Report <span class="sid">{sid}</span></h1>

<section><h2>1 · User goal</h2><p class="goal">{goal}</p></section>

<section><h2>2 · Planner DAG</h2>
<pre class="mermaid">{mermaid}</pre>
<details><summary>text</summary><pre>{dag_text}</pre></details>
</section>

<section><h2>3 + 4 · Browser path &amp; actions</h2>
{_browser_table_html(report.get("3_browser_paths") or [])}
</section>

<section><h2>5 · Screenshots / page-state logs</h2>
{_gallery_html(report)}
</section>

<section><h2>6 · Extracted data</h2>
{_extracted_html(report.get("6_extracted_data") or [])}
</section>

<section><h2>7 · Final comparison table</h2>
{_final_table_html(report)}
</section>

<section><h2>8 · Turns &amp; cost</h2>
<p>{stats}</p>
{_cost_table_html(report.get("8_cost_by_agent") or {})}
</section>
</body>
</html>
"""


def print_console(report: dict[str, Any]) -> None:
    print(f"\n{'=' * 78}")
    print(f"REPLAY REPORT — session {report['session_id']}")
    print(f"{'=' * 78}\n")

    print("1. USER GOAL")
    print(f"   {report['1_user_goal']}\n")

    print("2. PLANNER DAG")
    print(report["2_planner_dag_text"])
    print()

    print("3. BROWSER PATHS")
    for b in report["3_browser_paths"]:
        print(f"   {b['node_id']}: path={b['path']} turns={b['turns']}")
    print()

    print("4. BROWSER ACTIONS")
    for item in report["4_browser_actions"]:
        print(f"   {item['node_id']}: {len(item['actions'])} action(s)")
    print()

    print("5. PAGE-STATE LOGS")
    arts = report["5_page_state_logs"]
    print(f"   {len(arts)} screenshot artifact(s)")
    print()

    print("6. EXTRACTED DATA")
    for d in report["6_extracted_data"]:
        print(f"   {d['node_id']}: {list(d['fields'].keys())}")
    print()

    print("7. FINAL TABLE")
    if report.get("7_final_table"):
        print(f"   {str(report['7_final_table'])[:600]}")
    print()

    print("8. TURNS + COST")
    print(f"   browser turns: {report['8_total_browser_turns']}")
    print(f"   cost: {json.dumps(report['8_cost_by_agent'], indent=2)}")
    print(f"\n{'=' * 78}\n")


def generate_report(session_id: str, *, write_html: bool = True) -> dict[str, Any]:
    store = SessionStore(session_id)
    if not store.dir.is_dir():
        raise FileNotFoundError(f"Session not found: {session_id}")

    report = build_report(session_id)
    print_console(report)

    if write_html:
        html_out = render_html(report, store)
        out_path = store.dir / "REPORT.html"
        out_path.write_text(html_out, encoding="utf-8")
        print(f"Wrote {out_path}")

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate replay report for a session.")
    parser.add_argument("session_id", nargs="?", help="Session id under state/sessions/")
    parser.add_argument(
        "--list", action="store_true", help="List available session ids"
    )
    args = parser.parse_args(argv)

    if args.list:
        for sid in list_sessions():
            print(sid)
        return 0

    if not args.session_id:
        parser.print_help()
        return 2

    try:
        generate_report(args.session_id)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
