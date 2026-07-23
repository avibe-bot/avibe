"""Contract for the selected-row native resume helper across backends.

Claude (``SessionHandler``) and Codex (``BaseAgent``) resume from the native
session bound to the selected persisted row (by PK), whether selected explicitly
by Workbench or resolved for an IM run. Both helpers are static, so no heavy
SDK/controller setup is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.handlers.session_handler import SessionHandler
from modules.agents.base import BaseAgent

# Both helpers read the same payload path, so one parametrized suite covers them.
HELPERS = [BaseAgent._reserved_native_session_id, SessionHandler._reserved_native_session_id]


def _ctx(platform_specific):
    return SimpleNamespace(platform_specific=platform_specific)


@pytest.mark.parametrize("helper", HELPERS)
def test_returns_reserved_native_when_present(helper):
    ctx = _ctx({"agent_session_target": {"id": "ses-1", "native_session_id": "native-abc"}})
    assert helper(ctx) == "native-abc"


@pytest.mark.parametrize("helper", HELPERS)
def test_returns_resolved_im_run_native_when_present(helper):
    ctx = _ctx(
        {
            "agent_run_target": {
                "agent_session_id": "ses-topic",
                "native_session_id": "native-topic",
                "agent_backend": "claude",
            }
        }
    )
    assert helper(ctx) == "native-topic"


@pytest.mark.parametrize("helper", HELPERS)
def test_strips_whitespace(helper):
    ctx = _ctx({"agent_session_target": {"id": "ses-1", "native_session_id": "  native-abc  "}})
    assert helper(ctx) == "native-abc"


@pytest.mark.parametrize("helper", HELPERS)
def test_none_when_no_reserved_target(helper):
    # A context with no selected row falls back to the projection lookup.
    assert helper(_ctx({})) is None
    assert helper(_ctx(None)) is None
    assert helper(SimpleNamespace(platform_specific=None)) is None


@pytest.mark.parametrize("helper", HELPERS)
def test_none_when_native_absent_or_empty(helper):
    # First turn of a session: reserved row exists but no native captured yet.
    assert helper(_ctx({"agent_session_target": {"id": "ses-1"}})) is None
    assert helper(_ctx({"agent_session_target": {"id": "ses-1", "native_session_id": ""}})) is None
    assert helper(_ctx({"agent_session_target": {"id": "ses-1", "native_session_id": "   "}})) is None
