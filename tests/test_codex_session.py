from modules.agents.codex.session import CodexSessionManager


def test_all_base_sessions_keeps_invalidated_sessions_visible():
    manager = CodexSessionManager()
    manager.set_session_key("session-1", "slack::C1")
    manager.set_cwd("session-1", "/tmp/work")
    manager.set_agent_session_id("session-1", "ses-1")
    manager.set_thread_id("session-1", "thread-1")

    manager.invalidate_thread("session-1")

    assert manager.all_base_sessions() == ["session-1"]
    assert manager.get_agent_session_id("session-1") == "ses-1"


def test_clear_all_counts_sessions_without_live_thread_ids():
    manager = CodexSessionManager()
    manager.set_session_key("session-1", "slack::C1")
    manager.set_cwd("session-1", "/tmp/work")
    manager.set_agent_session_id("session-1", "ses-1")
    manager.set_thread_id("session-1", "thread-1")
    manager.invalidate_thread("session-1")

    assert manager.clear_all() == 1
    assert manager.get_agent_session_id("session-1") is None
