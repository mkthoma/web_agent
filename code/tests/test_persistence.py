"""Session store encoding and delete helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from persistence import (
    SESSIONS_ROOT,
    SessionStore,
    delete_all_sessions,
    delete_session,
    list_sessions,
    validate_session_id,
)
from schemas import NodeState


@pytest.fixture
def sessions_root(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    monkeypatch.setattr("persistence.SESSIONS_ROOT", root)
    return root


def test_write_node_roundtrips_unicode(sessions_root):
    sid = "webagent-unicode-test"
    store = SessionStore(sid)
    store.write_node(
        NodeState(
            node_id="n:1",
            skill="browser",
            status="complete",
            prompt_sent="Plan selected ✓ with arrow → ok",
        )
    )
    loaded = store.read_node("n:1")
    assert loaded is not None
    assert "✓" in (loaded.prompt_sent or "")


def test_validate_session_id_rejects_traversal():
    with pytest.raises(ValueError):
        validate_session_id("../evil")
    with pytest.raises(ValueError):
        validate_session_id("foo/bar")


def test_delete_session_and_all(sessions_root):
    SessionStore("webagent-a").write_query("q1")
    SessionStore("webagent-b").write_query("q2")
    assert set(list_sessions()) == {"webagent-a", "webagent-b"}

    assert delete_session("webagent-a") is True
    assert list_sessions() == ["webagent-b"]

    assert delete_session("missing-id") is False

    assert delete_all_sessions() == 1
    assert list_sessions() == []
