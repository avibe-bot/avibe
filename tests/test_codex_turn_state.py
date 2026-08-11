import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TURN_STATE_PATH = Path(__file__).resolve().parents[1] / "modules/agents/codex/turn_state.py"
_SPEC = importlib.util.spec_from_file_location("test_codex_turn_state_module", _TURN_STATE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
CodexTurnRegistry = _MODULE.CodexTurnRegistry


def test_finalize_turn_start_response_skips_completed_bootstrapped_turn():
    registry = CodexTurnRegistry()
    request = SimpleNamespace(base_session_id="session-1")

    registry.begin_turn_start(request, "thread-1")
    registry.bootstrap_turn("turn-1", "session-1", "thread-1")
    registry.pop_turn("turn-1")

    state = registry.finalize_turn_start_response("turn-1", request)

    assert state is None
    assert registry.get_turn("turn-1") is None
    assert registry.get_active_turn("session-1") is None


def test_finalize_turn_start_response_registers_live_turn():
    registry = CodexTurnRegistry()
    request = SimpleNamespace(base_session_id="session-1")

    registry.begin_turn_start(request, "thread-1")

    state = registry.finalize_turn_start_response("turn-1", request)

    assert state is not None
    assert registry.get_turn("turn-1") is state
    assert registry.get_active_turn("session-1") == "turn-1"


def test_finalize_turn_start_response_prefers_response_turn_id_over_stale_pending_id():
    registry = CodexTurnRegistry()
    request = SimpleNamespace(base_session_id="session-1")

    registry.begin_turn_start(request, "thread-1")
    registry.bootstrap_turn("turn-old", "session-1", "thread-1")
    registry.pop_turn("turn-old")

    state = registry.finalize_turn_start_response("turn-new", request)

    assert state is not None
    assert registry.get_turn("turn-old") is None
    assert registry.get_turn("turn-new") is state
    assert registry.get_active_turn("session-1") == "turn-new"


def test_finalize_turn_start_response_removes_old_bootstrapped_active_turn():
    registry = CodexTurnRegistry()
    request = SimpleNamespace(base_session_id="session-1")

    registry.begin_turn_start(request, "thread-1")
    registry.bootstrap_turn("turn-old", "session-1", "thread-1")

    state = registry.finalize_turn_start_response("turn-new", request)

    assert state is not None
    assert registry.get_turn("turn-old") is None
    assert registry.get_request_for_turn("turn-old") is None
    assert registry.get_active_turn("session-1") == "turn-new"


def test_indicator_cleanup_has_exactly_one_owner():
    registry = CodexTurnRegistry()
    request = SimpleNamespace(base_session_id="session-1")
    registry.register_turn("turn-1", request)

    assert registry.claim_indicator_cleanup("turn-1") is request
    assert registry.claim_indicator_cleanup("turn-1") is None
