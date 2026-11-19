from types import SimpleNamespace

from core import gc_census, loop_watchdog


def test_warm_arm_needs_new_idle_full_collection_after_two_hours(monkeypatch):
    t = [100.0]
    w = loop_watchdog.EventLoopWatchdog(SimpleNamespace(), monotonic=lambda: t[0])
    samples = []
    monkeypatch.setattr(gc_census, "sample_controller", lambda _, **meta: samples.append(meta))
    assert w.arm_warm_census(object()) and not w.arm_warm_census(object())

    def gc_at(start, end, gen, streams):
        monkeypatch.setattr(loop_watchdog, "_active_requests", streams)
        t[0] = start
        info = dict(generation=gen, candidates=432, collected=0, uncollectable=0)
        w._observe_gc("start", info)
        t[0] = end
        w._observe_gc("stop", info)

    gc_at(7299.99, 7300, 2, 0)
    assert not w._census_due
    gc_at(7301, 7301, 2, 1)
    gc_at(7302, 7302, 1, 0)
    assert not w._census_due
    gc_at(7303, 7303, 2, 0)
    assert w._census_due and not samples
    w._census()
    assert samples == [dict(trigger_candidates=432, trigger_uptime_s=7203, sample_uptime_s=7203)]
