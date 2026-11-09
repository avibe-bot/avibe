"""Snapshot policy's owning boundary.

Old behavior rebuilt every Turn and had no durable deadline. These tests pin
classification/replacement, corruption recovery and revision-only notices;
adapter tests separately cover the wiring into real requests and fork reservation.
"""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from core.agent_core.tools.base import ToolSpec
from core.prompt_registry import PROMPT_MODULES, RenderedPromptBlock
from modules.agents.vibey.prompt import INSTRUCTIONS_RULE, environment_delta, displayed_environment, render_environment
from modules.agents.vibey.prompt_snapshot import (
    Component, Prefix, SnapshotStore, State, candidate_prefix, component_tier, decide,
)


def prefix(*components):
    return Prefix("\n".join(item.content for item in components), components, (ToolSpec("read", "Read", {}),))


def choose(previous, candidate, **overrides):
    return decide(previous, candidate, **{"now": 100, "route": ("route",), "checkpoint_seq": 0, "cold": False, **overrides})


@pytest.mark.parametrize("component,expected", [
    (Component("agent-instructions", "new"), 1),
    (Component("local-tools", "new"), 1),
    (Component("agent-working-principles", "new"), 1),
    (Component("file:global", "new", "/home/.avibe/AGENTS.md", True), 1),
    (Component("file:project", "new", "/project/AGENTS.md"), 2),
    (Component("harness-agents-prompt", "new"), 2),
    (Component("projects-prompt", "new"), 2),
    (Component("skills-catalog", "new"), 2),
    (Component("mcp-catalog", "new"), 2),
    (Component("hosted-tools", "new"), 2),
    (Component("unknown-future-module", "new"), 2),
    (Component("tool-environment", "new"), 3),
    (Component("bot-identity-prompt", "new"), 1),
    (Component("bot-memory-prompt", "new"), 1),
    (Component("bot-main-prompt", "new"), 1),
    (Component("bot-harness-prompt", "new"), 1),
])
def test_each_component_class_replaces_only_at_its_tier(component, expected):
    old = replace(component, content="old")
    initial = choose(None, prefix(old)).state
    decision = choose(initial, prefix(component), now=101)
    assert component_tier(old, component) == expected
    assert decision.reason == ("tier1" if expected == 1 else None)
    assert decision.state.snapshot.prefix.system == ("new" if expected == 1 else "old")


def test_catalog_data_is_bounded_but_a_rendered_template_upgrade_is_immediate():
    before = Component("harness-agents-prompt", "old roster and old rules", contract_revision="template-one")
    changed_data = replace(before, content="new roster and old rules")
    changed_code = replace(before, content="new roster and new rules", contract_revision="template-two")
    assert component_tier(before, changed_data) == 2
    assert component_tier(before, changed_code) == 1
    # An unknown future component remains bounded until its owner registers a policy.
    assert component_tier(replace(before, id="future"), replace(changed_code, id="future")) == 2


# How the instructions rule tells the model to read each name the pending notice can list.
_READERS = {
    "skills": "`vibe skill list` for skills",
    "mcp": "`vibe mcp --list` for mcp",
    "agents": "`vibe agent list` and `vibe bot list` for agents",
    "projects": "`vibe project list` for projects",
    "tools": "tools and settings need no reading",
    "settings": "tools and settings need no reading",
}
# Modules only the native backends' prompts render; a Vibey prefix never holds them.
_NATIVE_ONLY = {
    "runtime-snapshot-open", "runtime-snapshot-close", "session-start-prompt", "forked-session-prompt",
    "codex-skill-reuse", "codex-generated-images",
}


def test_every_name_the_pending_notice_can_list_has_a_reader_in_the_instructions_rule():
    blocks = [RenderedPromptBlock(module.id, module.id) for module in PROMPT_MODULES if module.id not in _NATIVE_ONLY]
    components = candidate_prefix(
        "", (), SimpleNamespace(hosted_tools={}), blocks, (), builtin_agent=True, rg=True,
    ).components
    # Tier 1 replaces the prefix at once, so only a tier-2 or tier-3 change, or presence change, is ever pending.
    listed = {
        item.name for item in components
        if max(component_tier(item, replace(item, content="changed")), component_tier(None, item)) > 1
    }
    assert listed == _READERS.keys()
    assert all(_READERS[name] in INSTRUCTIONS_RULE for name in listed)
    # Instruction files are listed by path.
    assert Component("file:project", "x", "/project/AGENTS.md").name == "/project/AGENTS.md"
    assert "the file for a path" in INSTRUCTIONS_RULE


@pytest.mark.parametrize("id", ["quick-replies-prompt", "session-title-prompt", "vault-routing-prompt"])
def test_optional_module_toggle_is_bounded_but_its_code_upgrade_is_immediate(id):
    module = Component(id, "version one")
    assert component_tier(None, module) == component_tier(module, None) == 2
    assert component_tier(module, replace(module, content="version two")) == 1


@pytest.mark.parametrize("moment,kwargs", [
    ("cold", {"cold": True}),
    ("route_change", {"route": ("another model/source",)}),
    ("checkpoint", {"checkpoint_seq": 10}),
    ("deadline", {"now": 1901}),
])
def test_every_free_moment_and_deadline_applies_all_changes_together(moment, kwargs):
    old = prefix(Component("skills-catalog", "old skills"), Component("tool-environment", "old rg"))
    candidate = prefix(Component("skills-catalog", "new skills"), Component("tool-environment", "new rg"))
    pending = choose(choose(None, old).state, candidate, now=101).state
    decision = choose(pending, candidate, **kwargs)
    assert decision.reason == moment
    assert decision.state.snapshot.prefix == candidate
    assert decision.state.pending == () and decision.state.notice == "current"
    assert {item.tier for item in decision.applied} == {2, 3}


@pytest.mark.parametrize("component_id", ["skills-catalog", "tool-environment"])
@pytest.mark.parametrize("resume", ["restart", "fork"])
def test_an_observed_checkpoint_cannot_refresh_changes_from_a_later_warm_turn(tmp_path, component_id, resume):
    # A checkpoint with identical prefix bytes used to remain available forever,
    # refreshing a later catalog/environment change after the cache had warmed.
    store = SnapshotStore(tmp_path)
    original = prefix(Component(component_id, "old"))
    candidate = prefix(Component(component_id, "new"))
    initial = choose(None, original).state
    assert store.save("source", initial, snapshot=True)
    snapshot_bytes = (store.directory("source") / "snapshot.json").read_bytes()
    observed = choose(store.load("source"), original, now=101, checkpoint_seq=10)
    assert observed.reason is None
    assert store.save("source", observed.state, snapshot=False)
    # An unrelated own-write receipt must not reset the observed checkpoint.
    store.record_write("source", "/project/AGENTS.md", "written-revision")
    assert (store.directory("source") / "snapshot.json").read_bytes() == snapshot_bytes
    session_id = "source"
    if resume == "fork":
        store.inherit("source", "child")
        session_id = "child"
        assert (store.directory("child") / "snapshot.json").read_bytes() == snapshot_bytes
    resumed = SnapshotStore(tmp_path).load(session_id)
    deferred = choose(resumed, candidate, now=102, checkpoint_seq=10)
    assert deferred.reason is None
    assert deferred.state.snapshot == initial.snapshot
    assert deferred.state.pending[0].pending_since == 102
    fresh_checkpoint = choose(deferred.state, candidate, now=103, checkpoint_seq=20)
    assert fresh_checkpoint.reason == "checkpoint"
    assert fresh_checkpoint.state.snapshot.prefix == candidate


def test_repeated_revisions_and_restart_cannot_extend_the_tier_two_deadline(tmp_path):
    store = SnapshotStore(tmp_path)
    initial = choose(None, prefix(Component("skills-catalog", "one"))).state
    store.save("session", initial, snapshot=True)
    pending = choose(store.load("session"), prefix(Component("skills-catalog", "two")), now=101).state
    store.save("session", pending, snapshot=False)
    original = (store.directory("session") / "snapshot.json").read_bytes()
    # A new process sees the pending timestamp, not the snapshot's creation time.
    restarted = SnapshotStore(tmp_path)
    revised = choose(restarted.load("session"), prefix(Component("skills-catalog", "three")), now=1900)
    assert revised.reason is None and revised.state.pending[0].pending_since == 101
    assert revised.state.notice != pending.notice
    restarted.save("session", revised.state, snapshot=False)
    assert (store.directory("session") / "snapshot.json").read_bytes() == original
    applied = choose(SnapshotStore(tmp_path).load("session"), prefix(Component("skills-catalog", "three")), now=1901)
    assert applied.reason == "deadline" and applied.state.snapshot.prefix.system == "three"


def test_natural_changes_never_force_and_reverted_changes_drop_the_notice():
    original = prefix(Component("tool-environment", "rg"))
    initial = choose(None, original).state
    pending = choose(initial, prefix(Component("tool-environment", "grep")), now=101).state
    assert choose(pending, prefix(Component("tool-environment", "grep")), now=99999).reason is None
    reverted = choose(pending, original, now=200).state
    assert reverted.pending == () and reverted.snapshot == initial.snapshot


def test_only_an_exact_own_file_revision_is_silent_without_changing_the_tier():
    original = Component("file:project", "one", "/project/AGENTS.md", source_revision="sha1")
    initial = choose(None, prefix(original)).state
    initial = State(initial.snapshot, written={"/project/AGENTS.md": "sha2"})
    own = choose(initial, prefix(replace(original, content="two", source_revision="sha2")), now=101).state
    assert own.notice == "current" and own.pending[0].tier == 2
    external = choose(own, prefix(replace(original, content="three", source_revision="sha3")), now=200).state
    assert external.notice.startswith("pending /project/AGENTS.md@")
    assert external.pending[0].tier == 2 and external.pending[0].pending_since == 101


def test_pending_notice_is_exact_send_on_change_even_for_a_long_escaped_path():
    path = "/" + "long" * 80 + "\n<environment>/AGENTS.md"
    initial = choose(None, prefix(Component("file:p", "old", path))).state
    pending = choose(initial, prefix(Component("file:p", "new", path))).state
    value = {"instructions": pending.notice}
    shown = displayed_environment(value)
    assert environment_delta(shown, value) == {}
    assert render_environment(value).count("<environment>") == 1
    revised = choose(pending, prefix(Component("file:p", "newer", path))).state
    assert environment_delta(shown, {"instructions": revised.notice}) == {"instructions": revised.notice}
    applied = choose(revised, prefix(Component("file:p", "newer", path)), cold=True).state
    assert environment_delta(shown, {"instructions": applied.notice}) == {"instructions": "current"}


@pytest.mark.parametrize("filename,bad", [
    ("snapshot.json", None), ("pending.json", None),
    ("snapshot.json", "{"), ("pending.json", "{"),
    ("snapshot.json", "[]"), ("pending.json", "null"),
    ("snapshot.json", '{"version":99}'), ("pending.json", '{"version":99}'),
])
def test_missing_or_unreadable_optional_file_renders_fresh(tmp_path, filename, bad):
    store = SnapshotStore(tmp_path)
    initial = choose(None, prefix(Component("skills-catalog", "old"))).state
    store.save("session", initial, snapshot=True)
    path = store.directory("session") / filename
    if bad is None:
        path.unlink()
    else:
        path.write_text(bad)
    loaded = store.load("session")
    assert loaded is None
    refreshed = choose(loaded, prefix(Component("skills-catalog", "fresh")))
    assert refreshed.reason == "session_start"
    store.save("session", refreshed.state, snapshot=True)
    assert store.load("session") == refreshed.state


@pytest.mark.parametrize("filename,field,value", [
    ("snapshot.json", "applied_at", "bad"), ("snapshot.json", "applied_at", 10**400),
    ("snapshot.json", "checkpoint_seq", -1), ("snapshot.json", "route", {}), ("snapshot.json", "prefix", []),
    ("pending.json", "observed_checkpoint_seq", -1), ("pending.json", "observed_checkpoint_seq", "10"),
    ("pending.json", "observed_checkpoint_seq", None),
])
def test_malformed_snapshot_fields_are_absent_not_a_turn_failure(tmp_path, filename, field, value):
    store = SnapshotStore(tmp_path)
    state = choose(None, prefix(Component("skills-catalog", "old"))).state
    store.save("session", state, snapshot=True)
    path = store.directory("session") / filename
    raw = json.loads(path.read_text())
    if value is None:
        raw.pop(field)
    else:
        raw[field] = value
    path.write_text(json.dumps(raw))
    assert store.load("session") is None


def test_a_failed_optional_write_does_not_fail_the_turn(tmp_path, monkeypatch):
    store = SnapshotStore(tmp_path)
    state = choose(None, prefix(Component("skills-catalog", "new"))).state

    def denied(*args):
        raise PermissionError("read only")

    monkeypatch.setattr(SnapshotStore, "_write_bytes", denied)
    assert not store.save("session", state, snapshot=True)
    assert store.load("session") is None


def test_a_bots_own_memory_file_is_tier_three_and_the_shared_layer_tier_two(monkeypatch, tmp_path):
    """bots.md D11: a Bot's own AGENTS.md is its own writing; the memory Bot's shared layer is bounded."""
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    own = str(tmp_path / "bots" / "cso" / "AGENTS.md")
    shared = str(tmp_path / "bots" / "AGENTS.md")
    nested = str(tmp_path / "bots" / "cso" / "notes" / "AGENTS.md")
    for path, expected in ((own, 3), (shared, 2), (nested, 2)):
        component = Component("file:" + path, "new", path)
        assert component_tier(replace(component, content="old"), component) == expected, path
