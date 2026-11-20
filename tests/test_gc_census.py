from types import SimpleNamespace

from core import gc_census


def test_opt_in_is_explicit(monkeypatch):
    from core.loop_watchdog import EventLoopWatchdog

    monkeypatch.setenv("AVIBE_DIAGNOSTIC_GC_CENSUS", "1")
    watchdog = EventLoopWatchdog(SimpleNamespace())
    assert watchdog._census is None and watchdog.arm_warm_census(SimpleNamespace())


def test_count_only_private_types_shared_and_unattributed(monkeypatch):
    private = type("sensitive_type_name", (), {})()
    nested = [private]
    first = {"sensitive_value": nested}
    second = [nested]
    ctrl = SimpleNamespace(message_dispatcher=first, config=second)
    snapshot = [first, second, nested, private, ctrl]
    monkeypatch.setattr(gc_census.gc, "get_objects", lambda: snapshot)
    report = gc_census._counts(ctrl)
    assert report["shared"] == 2
    assert report["unattributed"] == 1
    assert report["owners"]["dispatcher"] == report["owners"]["config"] == 1
    assert "sensitive" not in str(report)
    assert sum(count for _, count in report["types"]) == 5


def test_full_container_leaves_no_budget_for_runtime_member(monkeypatch):
    import sys
    from types import ModuleType

    class Runtime:
        pass

    module = ModuleType("modules.agents.vibey.agent")
    module._SessionRuntime = Runtime
    monkeypatch.setitem(sys.modules, module.__name__, module)
    rt = Runtime()
    rt.run = []
    root = {("session",): rt}  # tracked referents include both the key and value
    ctrl = SimpleNamespace(config=root)
    monkeypatch.setattr(gc_census, "_EDGE_LIMIT", 2)
    monkeypatch.setattr(gc_census.gc, "get_objects", lambda: [root, rt, rt.run])
    report = gc_census._counts(ctrl)
    assert report["truncated"] and report["owners"]["config"] == 2
    assert report["unattributed"] == 1
