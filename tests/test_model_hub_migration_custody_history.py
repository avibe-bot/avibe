"""Completed custody history uses synthetic homes and guarded native stores."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import psutil
import pytest

from core.handlers.model_hub.adapter import OAuthCredentialRejectedError
from core.handlers.model_hub.migration import MigrationConflictError, prepare_takeover_reauthentication
from core.handlers.model_hub.migration_journal import NativeTakeoverJournal, TakeoverStateError
from core.handlers.model_hub.service import ModelHubError
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import (
    _service,
    _write,
    _write_claude_oauth,
    _write_codex_oauth,
)
from tests.test_model_hub_persisted_inventory import home  # noqa: F401 - hermetic fixture
from vibe import native_oauth_store


@pytest.fixture(autouse=True)
def _guard_native_boundaries(home, monkeypatch):
    # The shared home fixture also refuses Security store and subprocess calls.
    def forbidden(*args, **kwargs):
        raise AssertionError("real credential/process boundary reached")

    monkeypatch.setattr(psutil, "process_iter", forbidden)
    monkeypatch.setattr(native_oauth_store, "_security_bindings", forbidden)
    monkeypatch.setenv("USER", "fixture-user")
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")


def _oauth(home: Path, backend: str) -> Path:
    (_write_claude_oauth if backend == "claude" else _write_codex_oauth)(home)
    return home / (".claude/.credentials.json" if backend == "claude" else ".codex/auth.json")


def _api(home: Path, backend: str, key: str = "fixture-second") -> Path:
    if backend == "opencode":
        path = home / ".config/opencode/opencode.json"
        _write(path, json.dumps({"provider": {"openai": {"options": {"apiKey": key}}}}))
    elif backend == "claude":
        path = home / ".claude/settings.json"
        _write(path, json.dumps({"env": {"ANTHROPIC_API_KEY": key}}))
    else:
        path = home / ".codex/config.toml"
        _write(path, f'[model_providers.fixture]\nexperimental_bearer_token="{key}"\n')
    return path


def _empty(home: Path, backend: str) -> tuple[str, str]:
    if backend == "claude":
        locator = ("Claude Code-credentials", "fixture-user")
    else:
        _write(home / ".codex/config.toml", 'cli_auth_credentials_store="keyring"\n')
        account = "cli|" + hashlib.sha256(str((home / ".codex").resolve()).encode()).hexdigest()[:16]
        locator = ("Codex Auth", account)
    native_oauth_store._KEYCHAIN_STORE.items[locator] = (
        '{"mcpOAuth":{"keep":"保留"}}', "fixture-empty",
    )
    return locator


def _ids(service, backend: str | None = None) -> list[str]:
    return [
        row["id"] for row in service.migration_scan()["items"]
        if backend is None or row["backend"] == backend
    ]


def _apply(service, backend: str | None = None) -> dict:
    ids = _ids(service, backend)
    assert ids
    return asyncio.run(service.migration_apply(ids, clean_api_keys=True))


def _legacy(service) -> None:
    receipt = service.migration_journal.completed()
    receipt.pop("oauth_custody_backends")
    for row, identity in zip(receipt["items"], receipt.pop("inventory_ids"), strict=True):
        row["id"] = identity
    NativeTakeoverJournal(service.migration_journal.path.with_name("last-completed.json")).save(receipt)


def _counts(adapter) -> tuple[int, int, int]:
    return len(adapter.provisioned), len(adapter.oauth_provisioned), len(adapter.observed)


def _assert_refusal(service, store, adapter, paths: tuple[Path, ...], *, code=None) -> None:
    before = [path.read_bytes() for path in paths], store.config.to_payload()
    counts = _counts(adapter)
    with pytest.raises(ModelHubError) as failure:
        _apply(service)
    if code is not None:
        assert failure.value.code == code
    assert before == ([path.read_bytes() for path in paths], store.config.to_payload())
    assert _counts(adapter) == counts
    assert service.migration_journal.load() is None


@pytest.mark.parametrize("backend", ["claude", "codex"])
@pytest.mark.parametrize("middle", ["other_backend", "same_backend", "subset", "empty"])
@pytest.mark.parametrize("legacy", [False, True])
def test_completed_custody_survives_later_batches(home, tmp_path, backend, middle, legacy):
    native = _oauth(home, backend)
    original = native.read_bytes()
    if middle == "subset":
        other = _api(home, "opencode")
        other_original = other.read_bytes()
    service, store, adapter = _service(tmp_path, migration_home=home)
    _apply(service)
    if legacy:
        _legacy(service)
    if middle == "subset":
        other.write_bytes(other_original)
    elif middle == "empty":
        _empty(home, "codex" if backend == "claude" else "claude")
    else:
        _api(home, backend if middle == "same_backend" else "opencode")
    _apply(service)
    receipt = service.migration_journal.completed()
    assert not any(row["backend"] == backend and row["kind"] == "oauth_native" for row in receipt["items"])
    assert backend in receipt["oauth_custody_backends"]
    # A new controller must consume durable evidence, not an in-memory flag.
    restarted, _, _ = _service(tmp_path, migration_home=home)
    restarted.store, restarted.adapter = store, adapter
    native.write_bytes(original)
    _assert_refusal(
        restarted, store, adapter, (native,), code="migration_reauthorization_required",
    )
    assert len(adapter.oauth_provisioned) == 1


@pytest.mark.parametrize("backend", ["claude", "codex"])
@pytest.mark.parametrize("middle", ["api", "empty"])
@pytest.mark.parametrize("source_change", ["none", "ref", "deleted"])
def test_legacy_receipt_cannot_reconstruct_already_overwritten_oauth(
    home, tmp_path, backend, middle, source_change,
):
    native = _oauth(home, backend)
    original = native.read_bytes()
    service, store, adapter = _service(tmp_path, migration_home=home)
    _apply(service)
    source_id = store.config.sources[0].id
    if middle == "api":
        _api(home, "opencode")
    else:
        _empty(home, "codex" if backend == "claude" else "claude")
    _apply(service)
    # This is the on-disk shape left when an older release already overwrote
    # the original receipt before upgrade. No item-to-Source guess can fix it.
    _legacy(service)
    if middle == "empty":
        assert service.migration_journal.completed()["source_ids"] == []
    if source_change == "ref":
        store.config.sources[0].credential_ref = "cred_fixture_reauthenticated"
    elif source_change == "deleted":
        store.config.sources = [source for source in store.config.sources if source.id != source_id]
        for agent in store.config.agents.values():
            agent.sources.order = [value for value in agent.sources.order if value != source_id]
    native.write_bytes(original)
    _assert_refusal(service, store, adapter, (native,), code="migration_reauthorization_required")
    assert len(adapter.oauth_provisioned) == 1


@pytest.mark.parametrize("backend", ["claude", "codex"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("changed_sibling", [False, True])
def test_latest_exact_bundle_keeps_cleanup_only_and_whole_ref_guard(
    home, tmp_path, backend, legacy, changed_sibling,
):
    native = _oauth(home, backend)
    original = native.read_bytes()
    _api(home, "opencode")
    service, store, adapter = _service(tmp_path, migration_home=home)
    _apply(service)
    if legacy:
        _legacy(service)
    native.write_bytes(original)
    if changed_sibling:
        sibling = next(source for source in store.config.sources if source.kind == "api_key")
        sibling.credential_ref = "cred_fixture_reauthenticated"
        _assert_refusal(service, store, adapter, (native,), code="migration_item_conflict")
    else:
        counts = _counts(adapter)
        previous = store.config.to_payload()
        _apply(service)
        assert _counts(adapter) == counts
        assert store.config.to_payload() == previous
        assert service.migration_scan()["items"] == []
        expected = ["claude", "codex"] if legacy else [backend]
        assert service.migration_journal.completed()["oauth_custody_backends"] == expected


@pytest.mark.parametrize("first", ["api", "empty"])
@pytest.mark.parametrize("legacy", [False, True])
def test_explicit_empty_marker_distinguishes_new_from_unknown_legacy_history(home, tmp_path, first, legacy):
    if first == "api":
        _api(home, "opencode")
    else:
        _empty(home, "claude")
    service, store, adapter = _service(tmp_path, migration_home=home)
    _apply(service)
    assert service.migration_journal.completed()["oauth_custody_backends"] == []
    if legacy:
        _legacy(service)
    native = _oauth(home, "codex")
    if legacy:
        _assert_refusal(service, store, adapter, (native,), code="migration_reauthorization_required")
    else:
        _apply(service)
        assert len(adapter.oauth_provisioned) == 1
        assert service.migration_journal.completed()["oauth_custody_backends"] == ["codex"]


def test_new_history_allows_both_first_oauth_backends_and_unions_markers(home, tmp_path):
    _api(home, "opencode")
    service, _, adapter = _service(tmp_path, migration_home=home)
    _apply(service)
    for backend, expected in (("claude", ["claude"]), ("codex", ["claude", "codex"])):
        _oauth(home, backend)
        _apply(service)
        assert service.migration_journal.completed()["oauth_custody_backends"] == expected
    assert len(adapter.oauth_provisioned) == 2


def test_unknown_history_is_carried_through_later_api_and_empty_completions(home, tmp_path):
    _api(home, "opencode")
    service, store, adapter = _service(tmp_path, migration_home=home)
    _apply(service)
    _legacy(service)
    _api(home, "codex")
    _apply(service)
    _empty(home, "claude")
    _apply(service)
    assert service.migration_journal.completed()["source_ids"] == []
    assert service.migration_journal.completed()["oauth_custody_backends"] == ["claude", "codex"]
    native = _oauth(home, "codex")
    _assert_refusal(service, store, adapter, (native,), code="migration_reauthorization_required")
    assert not adapter.oauth_provisioned


def test_prior_custody_refusal_precedes_every_proof_in_a_fresh_mixed_selection(home, tmp_path):
    native = _oauth(home, "codex")
    original = native.read_bytes()
    service, store, adapter = _service(tmp_path, migration_home=home)
    _apply(service)
    _api(home, "opencode")
    _apply(service)
    native.write_bytes(original)
    other = _api(home, "claude")
    _assert_refusal(service, store, adapter, (native, other), code="migration_reauthorization_required")


@pytest.mark.parametrize("change", ["metadata", "tokens"])
def test_earlier_backend_custody_never_guesses_new_native_authorization(home, tmp_path, change):
    native = _oauth(home, "claude")
    original = json.loads(native.read_text())
    service, store, adapter = _service(tmp_path, migration_home=home)
    _apply(service)
    _api(home, "opencode")
    _apply(service)
    if change == "metadata":
        original["fixture_metadata"] = True
    else:
        original["claudeAiOauth"].update({
            "accessToken": "fixture-new-access", "refreshToken": "fixture-new-refresh",
        })
    _write(native, json.dumps(original))
    _assert_refusal(service, store, adapter, (native,), code="migration_reauthorization_required")


@pytest.mark.parametrize("outcome", ["needs_auth", "reauth_requested"])
def test_terminal_outcome_keeps_backend_fence_after_another_batch(home, tmp_path, outcome):
    native = _oauth(home, "claude")
    original = native.read_bytes()
    service, store, adapter = _service(tmp_path, migration_home=home)
    validate = adapter.validate_oauth_credential

    async def unavailable(ref):
        if outcome == "needs_auth":
            raise OAuthCredentialRejectedError()
        raise RuntimeError("fixture inconclusive validation")

    adapter.validate_oauth_credential = unavailable
    with pytest.raises(ModelHubError):
        _apply(service)
    if outcome == "reauth_requested":
        source = store.config.sources[0]
        asyncio.run(prepare_takeover_reauthentication(service, source.id))
    assert service.migration_journal.completed()["outcome"] == outcome
    assert service.migration_journal.completed()["oauth_custody_backends"] == ["claude"]
    adapter.validate_oauth_credential = validate
    _api(home, "opencode")
    _apply(service)
    native.write_bytes(original)
    _assert_refusal(service, store, adapter, (native,), code="migration_reauthorization_required")


def test_legacy_active_custody_recovers_without_reapplying_new_grant_gate(home, tmp_path):
    _api(home, "opencode")
    service, store, adapter = _service(tmp_path, migration_home=home)
    _apply(service)
    native = _oauth(home, "claude")
    original = native.read_bytes()
    validate = adapter.validate_oauth_credential

    async def unavailable(ref):
        raise RuntimeError("fixture validation interrupted before upgrade")

    adapter.validate_oauth_credential = unavailable
    with pytest.raises(ModelHubError):
        _apply(service)
    assert service.migration_journal.load()["phase"] == "exposed"
    # Model an old release's active handoff plus its older marker-free
    # receipt. Recovery already owns that grant; this is not fresh consent.
    _legacy(service)
    adapter.validate_oauth_credential = validate
    restarted, _, _ = _service(tmp_path, migration_home=home)
    restarted.store, restarted.adapter = store, adapter
    counts = _counts(adapter)
    asyncio.run(restarted.recover_runtime_intent())
    assert _counts(adapter) == counts
    assert restarted.migration_journal.load() is None
    assert restarted.migration_journal.completed()["oauth_custody_backends"] == ["claude", "codex"]
    native.write_bytes(original)
    _apply(restarted)
    assert _counts(adapter) == counts
    assert restarted.migration_scan()["items"] == []


@pytest.mark.parametrize("first", ["api", "oauth", "empty"])
def test_first_receipt_interrupted_forget_preserves_explicit_new_history(home, tmp_path, monkeypatch, first):
    if first == "api":
        _api(home, "opencode")
    elif first == "oauth":
        _oauth(home, "claude")
    else:
        _empty(home, "claude")
    service, store, adapter = _service(tmp_path, migration_home=home)
    assert service.migration_journal.completed() is None
    ids = _ids(service)

    def crash():
        raise OSError("fixture first receipt saved before forgetting")

    with monkeypatch.context() as patch:
        patch.setattr(service.migration_journal, "forget", crash)
        with pytest.raises(ModelHubError):
            asyncio.run(service.migration_apply(ids, clean_api_keys=True))
    expected = ["claude"] if first == "oauth" else []
    assert service.migration_journal.completed()["oauth_custody_backends"] == expected
    counts = _counts(adapter)
    restarted, _, _ = _service(tmp_path, migration_home=home)
    restarted.store, restarted.adapter = store, adapter
    if first == "empty":
        # This source-free transaction never became exposed. Repeated forget
        # failure left rollback pending; recovery finishes it and reports the
        # original conflict once, without erasing the completed empty marker.
        assert restarted.migration_journal.load()["phase"] == "reverting"
        with pytest.raises(MigrationConflictError):
            asyncio.run(restarted.recover_runtime_intent())
        assert restarted.migration_journal.load() is None
    asyncio.run(restarted.recover_runtime_intent())
    assert asyncio.run(restarted.migration_apply(ids, clean_api_keys=True))["applied"] == 1
    assert _counts(adapter) == counts
    assert restarted.migration_journal.completed()["oauth_custody_backends"] == expected
    assert restarted.migration_journal.load() is None


@pytest.mark.parametrize("boundary", ["before_save", "after_save", "before_forget", "after_forget"])
@pytest.mark.parametrize("legacy,rejected", [(False, False), (True, False), (False, True)])
def test_completion_crash_replays_custody_union_once(home, tmp_path, monkeypatch, boundary, legacy, rejected):
    native = _oauth(home, "claude")
    original = native.read_bytes()
    service, store, adapter = _service(tmp_path, migration_home=home)
    _apply(service)
    if legacy:
        _legacy(service)
    if rejected:
        _oauth(home, "codex")

        async def reject(ref):
            raise OAuthCredentialRejectedError()

        adapter.validate_oauth_credential = reject
    else:
        _api(home, "opencode")
    ids = _ids(service)
    save, forget = NativeTakeoverJournal.save, NativeTakeoverJournal.forget
    triggered = False

    def crash_save(self, payload):
        nonlocal triggered
        if self.path.name == "last-completed.json" and boundary.endswith("save") and not triggered:
            triggered = True
            if boundary == "after_save":
                save(self, payload)
            raise OSError("fixture receipt publication crash")
        save(self, payload)

    def crash_forget(self):
        nonlocal triggered
        if self.path == service.migration_journal.path and boundary.endswith("forget") and not triggered:
            triggered = True
            if boundary == "after_forget":
                forget(self)
            raise OSError("fixture active journal removal crash")
        forget(self)

    with monkeypatch.context() as patch:
        patch.setattr(NativeTakeoverJournal, "save", crash_save)
        patch.setattr(NativeTakeoverJournal, "forget", crash_forget)
        with pytest.raises(ModelHubError):
            asyncio.run(service.migration_apply(ids, clean_api_keys=True))
    assert triggered
    counts = _counts(adapter)
    restarted, _, _ = _service(tmp_path, migration_home=home)
    restarted.store, restarted.adapter = store, adapter
    asyncio.run(restarted.recover_runtime_intent())
    if rejected:
        with pytest.raises(ModelHubError):
            asyncio.run(restarted.migration_apply(ids, clean_api_keys=True))
    else:
        assert asyncio.run(restarted.migration_apply(ids, clean_api_keys=True))["applied"] == 1
    assert _counts(adapter) == counts
    assert restarted.migration_journal.load() is None
    receipt = restarted.migration_journal.completed()
    assert receipt["oauth_custody_backends"] == (["claude", "codex"] if legacy or rejected else ["claude"])
    assert receipt["outcome"] == ("needs_auth" if rejected else "success")
    native.write_bytes(original)
    _assert_refusal(restarted, store, adapter, (native,), code="migration_reauthorization_required")


@pytest.mark.parametrize("marker", [
    None, "", {}, 1, True, ["opencode"], ["unknown"], [None], [[]], [{"bad": True}],
    ["claude", "claude"], ["claude", "codex", "claude"],
])
@pytest.mark.parametrize("operation", ["load", "save"])
def test_invalid_custody_evidence_is_not_empty_or_legacy(home, tmp_path, marker, operation):
    _api(home, "opencode")
    service, store, adapter = _service(tmp_path, migration_home=home)
    _apply(service)
    receipt = service.migration_journal.completed()
    receipt["oauth_custody_backends"] = marker
    journal = NativeTakeoverJournal(service.migration_journal.path.with_name("last-completed.json"))
    if operation == "save":
        original = journal.path.read_bytes()
        with pytest.raises(TakeoverStateError):
            journal.save(receipt)
        assert journal.path.read_bytes() == original
    else:
        journal.path.write_text(json.dumps(receipt))
        original = journal.path.read_bytes()
        with pytest.raises(TakeoverStateError):
            journal.load()
        counts, previous = _counts(adapter), store.config.to_payload()
        native = _oauth(home, "claude")
        before = native.read_bytes()
        with pytest.raises(ModelHubError):
            asyncio.run(service.migration_apply(["mig_fixture_untrusted"], clean_api_keys=True))
        assert native.read_bytes() == before
        assert _counts(adapter) == counts and store.config.to_payload() == previous
        assert journal.path.read_bytes() == original


def test_present_marker_cannot_hide_current_bundle_oauth_evidence(home, tmp_path):
    _oauth(home, "claude")
    service, _, _ = _service(tmp_path, migration_home=home)
    _apply(service)
    receipt = service.migration_journal.completed()
    receipt["oauth_custody_backends"] = []
    assert NativeTakeoverJournal.oauth_custody_backends(receipt) == frozenset({"claude"})
    assert "oauth_custody_backends" not in json.dumps(service.migration_scan())
    stored = service.migration_journal.path.with_name("last-completed.json").read_text()
    assert "claude-refresh-token" not in stored and "claude-oauth-token" not in stored
