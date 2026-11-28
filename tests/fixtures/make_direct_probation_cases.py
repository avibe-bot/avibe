"""The reference model of the direct path's probation, and the cases it writes.

``docs/plans/remote-direct-probation.md`` is the rule. This file is that rule
as about sixty lines of Python (``Guard``), and the scenarios both shells run
against their own implementation (``direct_probation_cases.json``, written by
``python tests/fixtures/make_direct_probation_cases.py``).

The expected values in the JSON are this model's, never either shell's. Where
a number is the point of a case (how often a flapping path is adopted), the
case also carries ``milestones`` typed by hand below: writing the file fails
when the model disagrees with one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VERSION = 1
LEVELS_S = (0, 30, 600)
EARLY_MS = 600_000
HELD_MS = 600_000
EXPIRY_MS = 1_800_000
CAUSES = ("lost", "background", "choice", "superseded", "instance_ended", "network_change")
OUTPUT = Path(__file__).with_name("direct_probation_cases.json")


class Guard:
    """One device's probation state: a level per account and instance."""

    def __init__(self) -> None:
        self.stored: dict[str, dict[str, Any]] = {}  # what survives a relaunch
        self.links: dict[str, dict[str, Any]] = {}
        self.foreground = False
        self.stretch = 0

    def _key(self, key: str) -> dict[str, Any]:
        return self.stored.setdefault(key, {"level": 0, "rose": "none", "last_early_wall": None})

    def _reset(self, state: dict[str, Any]) -> None:
        state.update(level=0, rose="none", last_early_wall=None)

    def _settle(self, key: str, now: int, wall: int) -> dict[str, Any]:
        """Apply what time alone decides: the expiry, then "held"."""
        state = self._key(key)
        last = state["last_early_wall"]
        if state["level"] > 0 and last is not None:
            if wall < last:
                state["last_early_wall"] = wall  # the clock went back: no credit
            elif wall - last >= EXPIRY_MS:
                self._reset(state)
        link = self.links.get(key)
        if link and link["adopted_at"] is not None and now - link["adopted_at"] >= HELD_MS:
            self._reset(state)
        return state

    def to_foreground(self) -> None:
        self.foreground = True
        self.stretch += 1

    def to_background(self) -> None:
        self.foreground = False

    def relaunch(self) -> None:
        self.stored = json.loads(json.dumps(self.stored))
        self.links = {}
        self.foreground = False

    def link_up(self, key: str, link: int, now: int, wall: int, strict: bool = False) -> int:
        state = self._settle(key, now, wall)
        wait = 0 if strict else LEVELS_S[state["level"]] * 1000
        self.links[key] = {
            "id": link, "up_at": now, "wait": wait, "adopted_at": None,
            "stretch": self.stretch if self.foreground else None,
        }
        return wait

    def adopt(self, key: str, link: int, now: int) -> bool:
        current = self.links.get(key)
        if (
            not current or current["id"] != link or current["adopted_at"] is not None
            or not self.foreground or current["stretch"] != self.stretch
            or now - current["up_at"] < current["wait"]
        ):
            return False
        current["adopted_at"] = now
        return True

    def link_down(self, key: str, link: int, cause: str, now: int, wall: int) -> None:
        assert cause in CAUSES, cause
        current = self.links.get(key)
        if not current or current["id"] != link:
            return  # an older link's late close
        state = self._settle(key, now, wall)
        del self.links[key]
        if cause != "lost" or not self.foreground or current["stretch"] != self.stretch:
            return
        adopted = current["adopted_at"]
        if adopted is not None and now - adopted >= EARLY_MS:
            return  # it held: settled to level 0 above
        state["level"] = min(state["level"] + 1, len(LEVELS_S) - 1)
        state["rose"] = "probation" if adopted is None else "adopted"
        state["last_early_wall"] = wall

    def read(self, key: str, now: int, wall: int) -> dict[str, Any]:
        state = self._settle(key, now, wall)
        link = self.links.get(key)
        return {
            "level_s": LEVELS_S[state["level"]],
            "rose": state["rose"],
            "usable": bool(link and link["adopted_at"] is not None),
        }


def run(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run a case's events through the model and write each one's ``expect``."""
    guard = Guard()
    offset = 0
    last_at = 0
    adoptions: dict[str, int] = {}
    out = []
    for raw in events:
        event = dict(raw)
        kind, at = event["event"], event["at_ms"]
        assert at >= last_at, f"time runs back at {event}"
        last_at = at
        if kind == "clock_back":
            offset -= event["by_ms"]
        elif kind == "foreground":
            guard.to_foreground()
        elif kind == "background":
            guard.to_background()
        elif kind == "relaunch":
            guard.relaunch()
        else:
            key, wall = event["key"], at + offset
            expect: dict[str, Any] = {}
            if kind == "link_up":
                expect["wait_ms"] = guard.link_up(key, event["link"], at, wall, event.get("strict", False))
            elif kind == "adopt":
                expect["adopted"] = guard.adopt(key, event["link"], at)
                adoptions[key] = adoptions.get(key, 0) + int(expect["adopted"])
            elif kind == "link_down":
                guard.link_down(key, event["link"], event["cause"], at, wall)
            else:
                assert kind == "check", kind
            expect.update(guard.read(key, at, wall), adoptions=adoptions.get(key, 0))
            event["expect"] = expect
        out.append(event)
    return out


# --- The cases -------------------------------------------------------------

K = "a1/i1"


class Script:
    """Writes a case's events in order; times are elapsed milliseconds."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.link = 0

    def add(self, at_s: float, event: str, **more: Any) -> "Script":
        self.events.append({"at_ms": round(at_s * 1000), "event": event, **more})
        return self

    def fg(self, at_s: float) -> "Script":
        return self.add(at_s, "foreground")

    def bg(self, at_s: float) -> "Script":
        return self.add(at_s, "background")

    def up(self, at_s: float, key: str = K, **more: Any) -> int:
        self.link += 1
        self.add(at_s, "link_up", key=key, link=self.link, **more)
        return self.link

    def adopt(self, at_s: float, link: int | None = None, key: str = K) -> "Script":
        return self.add(at_s, "adopt", key=key, link=link or self.link)

    def down(self, at_s: float, cause: str, link: int | None = None, key: str = K) -> "Script":
        return self.add(at_s, "link_down", key=key, link=link or self.link, cause=cause)

    def check(self, at_s: float, key: str = K) -> "Script":
        return self.add(at_s, "check", key=key)


def flapping(lives_s: float, total_s: float, redial_s: float = 5) -> list[dict[str, Any]]:
    """A path that dies ``lives_s`` after every link-up, dialled again
    ``redial_s`` later, with the shell's check run on time."""
    s = Script().fg(0)
    waits = {0: 0, 1: 30, 2: 600}
    level, at = 0, 0.0
    while at + lives_s <= total_s:
        s.up(at)
        wait = waits[level]
        if wait < lives_s:
            s.adopt(at + wait)
        s.down(at + lives_s, "lost")
        level = min(level + 1, 2)
        at += lives_s + redial_s
    s.check(total_s)
    return s.events


def cases() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def case(name: str, about: str, events: list[dict[str, Any]], milestones: list[dict[str, Any]] | None = None) -> None:
        result.append({"name": name, "about": about, "milestones": milestones or [], "events": events})

    case(
        "dies_every_20s",
        "A path that dies 20 s after every link-up, for 30 minutes: adopted once, then the tunnel for good.",
        flapping(20, 1800),
        [{"at_ms": 20_000, "key": K, "adoptions": 1, "level_s": 30},
         {"at_ms": 45_000, "key": K, "adoptions": 1, "level_s": 600},
         {"at_ms": 1_800_000, "key": K, "adoptions": 1, "level_s": 600}],
    )
    case(
        "dies_every_3min",
        "A path that dies 3 minutes after every link-up, for an hour: adopted twice, then it stays on the tunnel.",
        flapping(180, 3600),
        [{"at_ms": 180_000, "key": K, "adoptions": 1, "level_s": 30},
         {"at_ms": 365_000, "key": K, "adoptions": 2, "level_s": 600},
         {"at_ms": 3_600_000, "key": K, "adoptions": 2, "level_s": 600}],
    )

    s = Script().fg(0)
    s.up(0); s.adopt(0).check(1).check(3600)
    case("healthy", "A clean history: the first link is adopted at link-up and nothing changes in an hour.", s.events,
         [{"at_ms": 0, "key": K, "adoptions": 1, "level_s": 0}, {"at_ms": 3_600_000, "key": K, "adoptions": 1, "level_s": 0}])

    s = Script()
    at = 0.0
    for _ in range(5):
        s.fg(at); s.up(at); s.adopt(at); s.down(at + 50, "background").bg(at + 50)
        at += 60
    s.fg(at).check(at)
    case("five_returns_a_minute_apart",
         "In and out every minute on a healthy path: each return is adopted at once and the level never moves.",
         s.events,
         [{"at_ms": 50_000, "key": K, "adoptions": 1, "level_s": 0}, {"at_ms": 300_000, "key": K, "adoptions": 5, "level_s": 0}])

    s = Script().fg(0)
    s.up(0); s.adopt(0).down(20, "lost")
    s.up(25); s.adopt(54.999).adopt(55).check(654.999).check(655).down(700, "lost").check(701)
    case("held_10min_resets",
         "After one early death the next link waits 30 s, to the millisecond; held 10 minutes, the level is 0 again, "
         "and a death after that is not early.",
         s.events,
         [{"at_ms": 55_000, "key": K, "adoptions": 2, "level_s": 30}, {"at_ms": 655_000, "key": K, "adoptions": 2, "level_s": 0},
          {"at_ms": 701_000, "key": K, "adoptions": 2, "level_s": 0}])

    s = Script().fg(0)
    s.up(0); s.adopt(0).down(599.999, "lost")
    case("early_edge_before", "A death 1 ms before 10 minutes of adoption is early.", s.events,
         [{"at_ms": 599_999, "key": K, "adoptions": 1, "level_s": 30}])
    s = Script().fg(0)
    s.up(0); s.adopt(0).down(600, "lost")
    case("early_edge_at", "A death exactly 10 minutes after adoption is not early.", s.events,
         [{"at_ms": 600_000, "key": K, "adoptions": 1, "level_s": 0}])

    for cause in ("background", "choice", "superseded", "instance_ended", "network_change"):
        s = Script().fg(0)
        s.up(0); s.adopt(0).down(20, cause)
        if cause == "background":
            s.bg(20).fg(30)
        s.up(31); s.adopt(31).down(40, cause)
        case(f"own_close_{cause}",
             f"A link that ends by `{cause}` never raises the level, adopted or not, however soon.", s.events,
             [{"at_ms": 40_000, "key": K, "adoptions": 2, "level_s": 0}])

    s = Script().fg(0)
    s.up(0); s.adopt(0).down(20, "lost")
    s.up(25); s.down(40, "instance_ended")
    s.up(45); s.down(60, "lost").check(61)
    case("death_on_probation",
         "On probation an instance restart changes nothing; a loss raises the level with no adoption.", s.events,
         [{"at_ms": 40_000, "key": K, "adoptions": 1, "level_s": 30}, {"at_ms": 60_000, "key": K, "adoptions": 1, "level_s": 600}])

    s = Script().fg(0)
    s.up(0); s.adopt(0).down(20, "lost")
    s.up(25); s.down(65, "lost")
    case("late_check_is_no_adoption",
         "The wait has passed but the shell's check never ran: the link was never adopted, and its death is one on probation.",
         s.events, [{"at_ms": 65_000, "key": K, "adoptions": 1, "level_s": 600}])

    s = Script().fg(0)
    s.up(0); s.adopt(0).down(20, "lost")
    first = s.up(25)
    s.down(30, "superseded", link=first)
    s.up(30); s.down(31, "lost", link=first).check(32).adopt(60)
    case("old_link_late_close",
         "A newer answer replaced the link; the old link's late loss changes nothing and the new link is adopted on time.",
         s.events, [{"at_ms": 32_000, "key": K, "adoptions": 1, "level_s": 30}, {"at_ms": 60_000, "key": K, "adoptions": 2, "level_s": 30}])

    s = Script().fg(0)
    s.up(0); s.adopt(0).down(20, "lost").check(1819.999).check(1820)
    s.up(1821); s.adopt(1821)
    case("expiry_30min", "30 minutes after the last early death the level is 0, to the millisecond, and the next link is adopted at once.",
         s.events, [{"at_ms": 1_819_999, "key": K, "adoptions": 1, "level_s": 30}, {"at_ms": 1_820_000, "key": K, "adoptions": 1, "level_s": 0},
                    {"at_ms": 1_821_000, "key": K, "adoptions": 2, "level_s": 0}])

    s = Script().fg(0)
    s.up(0); s.adopt(0).down(20, "lost")
    s.up(25); s.down(45, "lost")
    s.up(1500); s.check(1846).adopt(2099.999).adopt(2100)
    case("wait_is_fixed_at_link_up",
         "The level expires while a link waits its 10 minutes: the wait it was given at link-up stands.", s.events,
         [{"at_ms": 1_846_000, "key": K, "adoptions": 1, "level_s": 0}, {"at_ms": 2_100_000, "key": K, "adoptions": 2, "level_s": 0}])

    s = Script().fg(0)
    s.up(0); s.adopt(0).down(20, "lost").bg(21)
    s.add(320, "relaunch").fg(320).check(320)
    s.up(321); s.adopt(321).adopt(351)
    case("relaunch_keeps_the_level", "A relaunch 5 minutes after an early death: the level is still 30 s.", s.events,
         [{"at_ms": 320_000, "key": K, "adoptions": 1, "level_s": 30}, {"at_ms": 351_000, "key": K, "adoptions": 2, "level_s": 30}])

    s = Script().fg(0)
    s.up(0); s.adopt(0).down(20, "lost").bg(21)
    s.add(1880, "relaunch").fg(1880).check(1880)
    s.up(1881); s.adopt(1881)
    case("relaunch_after_expiry", "A relaunch 31 minutes after the early death: the time away counted, the level is 0.", s.events,
         [{"at_ms": 1_880_000, "key": K, "adoptions": 1, "level_s": 0}, {"at_ms": 1_881_000, "key": K, "adoptions": 2, "level_s": 0}])

    s = Script().fg(0)
    s.up(0); s.adopt(0).down(20, "lost")
    s.add(600, "clock_back", by_ms=3_600_000).check(600).check(2399.999).check(2400)
    case("clock_set_back",
         "The wall clock is set back an hour: the expiry counts from the first read after it, with no credit for the time before.",
         s.events, [{"at_ms": 2_399_999, "key": K, "adoptions": 1, "level_s": 30}, {"at_ms": 2_400_000, "key": K, "adoptions": 1, "level_s": 0}])

    s = Script().fg(0)
    s.up(0); s.adopt(0).down(20, "lost")
    s.up(25, strict=True); s.adopt(25).down(45, "lost").check(46)
    s.up(50, strict=True); s.adopt(50)
    case("strict_is_used_at_once",
         "With the tunnel ruled out by the user a link is used at once whatever the level; its deaths still count.", s.events,
         [{"at_ms": 25_000, "key": K, "adoptions": 2, "level_s": 30}, {"at_ms": 46_000, "key": K, "adoptions": 2, "level_s": 600},
          {"at_ms": 50_000, "key": K, "adoptions": 3, "level_s": 600}])

    s = Script().fg(0)
    s.up(0); s.adopt(0).bg(100).fg(4000).down(4001, "lost").check(4002)
    s.up(4003); s.adopt(4003).bg(4010).down(4020, "lost").fg(4030).check(4030)
    case("lost_across_a_background_does_not_count",
         "A link that was up before the last return to the foreground (a computer that slept), or that is lost while "
         "in the background, does not raise the level.",
         s.events, [{"at_ms": 4_002_000, "key": K, "adoptions": 1, "level_s": 0}, {"at_ms": 4_030_000, "key": K, "adoptions": 2, "level_s": 0}])

    s = Script().fg(0)
    s.up(0); s.adopt(0).down(20, "lost")
    s.up(25); s.adopt(55).down(700, "background").bg(700)
    s.add(710, "relaunch").fg(710).check(710)
    case("held_is_stored_at_the_background",
         "A link held over 10 minutes and then closed for the background: level 0 is what a relaunch finds.", s.events,
         [{"at_ms": 710_000, "key": K, "adoptions": 2, "level_s": 0}])

    s = Script().fg(0)
    other = "a1/i2"
    s.up(0); s.adopt(0)
    s.up(0, key=other); s.adopt(0, key=other).down(20, "lost", key=other).check(21, key=other).check(21)
    case("two_instances_do_not_touch", "One instance's early death leaves the other's level alone.", s.events,
         [{"at_ms": 21_000, "key": other, "adoptions": 1, "level_s": 30}, {"at_ms": 21_000, "key": K, "adoptions": 1, "level_s": 0}])

    return result


def build() -> dict[str, Any]:
    built = []
    for case in cases():
        events = run(case["events"])
        for mark in case["milestones"]:
            seen = [e["expect"] for e in events
                    if e["at_ms"] <= mark["at_ms"] and e.get("key") == mark["key"] and "expect" in e]
            assert seen, f"{case['name']}: no keyed event at or before {mark['at_ms']}"
            got = {name: seen[-1][name] for name in ("adoptions", "level_s")}
            want = {name: mark[name] for name in ("adoptions", "level_s")}
            assert got == want, f"{case['name']} at {mark['at_ms']} ms: the model says {got}, the milestone {want}"
        built.append({**case, "events": events})
    return {
        "version": VERSION,
        "rule": {"levels_s": list(LEVELS_S), "early_ms": EARLY_MS, "held_ms": HELD_MS, "expiry_ms": EXPIRY_MS},
        "causes": list(CAUSES),
        "cases": built,
    }


def render() -> str:
    lines = ["{", f'  "version": {VERSION},']
    data = build()
    lines.append(f'  "rule": {json.dumps(data["rule"])},')
    lines.append(f'  "causes": {json.dumps(data["causes"])},')
    lines.append('  "cases": [')
    for index, case in enumerate(data["cases"]):
        lines.append("    {")
        lines.append(f'      "name": {json.dumps(case["name"])},')
        lines.append(f'      "about": {json.dumps(case["about"])},')
        lines.append(f'      "milestones": {json.dumps(case["milestones"])},')
        lines.append('      "events": [')
        rows = [f"        {json.dumps(event)}" for event in case["events"]]
        lines.append(",\n".join(rows))
        lines.append("      ]")
        lines.append("    }" + ("," if index + 1 < len(data["cases"]) else ""))
    lines += ["  ]", "}"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    OUTPUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUTPUT} ({len(build()['cases'])} cases)")
