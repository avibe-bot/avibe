from types import SimpleNamespace

from core import gc_census


def test_opt_in_is_explicit(monkeypatch):
    monkeypatch.delenv("AVIBE_DIAGNOSTIC_GC_CENSUS", raising=False)
    assert gc_census.configured(SimpleNamespace()) is None


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
