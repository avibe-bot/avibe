from __future__ import annotations

import json
from pathlib import Path

import pytest

from vibe import native_oauth_store as store


class FakeKeychain:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], tuple[str, str]] = {}
        self.metadata_denied = False
        self.read_denied = False
        self.delete_denied = False
        self.metadata_calls: list[tuple[str, str]] = []
        self.read_calls: list[tuple[str, str]] = []
        self.write_calls: list[tuple[str, str, str]] = []
        self.delete_calls: list[tuple[str, str]] = []

    def metadata(self, service: str, account: str) -> store._KeychainMetadata:
        self.metadata_calls.append((service, account))
        if self.metadata_denied:
            return store._KeychainMetadata("permission_needed", "denied")
        item = self.items.get((service, account))
        if item is None:
            return store._KeychainMetadata("not_found", "absent")
        return store._KeychainMetadata("found", item[1], {"acct": account, "svce": service})

    def read(self, service: str, account: str) -> store._KeychainRead:
        self.read_calls.append((service, account))
        if self.read_denied:
            raise store._KeychainDenied("denied")
        item = self.items.get((service, account))
        if item is None:
            raise store._KeychainNotFound("missing")
        return store._KeychainRead(item[0], item[1])

    def write(self, service: str, account: str, value: str) -> None:
        self.write_calls.append((service, account, value))
        self.items[(service, account)] = (value, f"write-{len(self.write_calls)}")

    def delete(self, service: str, account: str) -> None:
        self.delete_calls.append((service, account))
        if self.delete_denied:
            raise store._KeychainDenied("denied")
        self.items.pop((service, account), None)


@pytest.fixture
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeychain:
    fake = FakeKeychain()
    monkeypatch.setattr(store, "_KEYCHAIN_STORE", fake)
    return fake


def _codex_payload() -> dict:
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "id_token": "id-secret",
        },
        "last_refresh": "2026-09-20T00:00:00Z",
        "unrelated": {"keep": "yes"},
    }


def _claude_payload() -> dict:
    return {
        "claudeAiOauth": {
            "accessToken": "access-secret",
            "refreshToken": "refresh-secret",
            "expiresAt": 123,
        },
        "mcpOAuth": {"provider": "keep-me"},
    }


def test_codex_file_scan_is_exportable_and_cleanup_preserves_unrelated_fields(tmp_path: Path) -> None:
    auth_path = tmp_path / ".codex" / "auth.json"
    auth_path.parent.mkdir()
    auth_path.write_text(json.dumps(_codex_payload()), encoding="utf-8")

    scanned = store.read_native_oauth("codex", home=tmp_path)

    assert scanned is not None
    assert scanned.exportable is True
    assert scanned.keychain_edit is None
    assert scanned.payload == _codex_payload()

    secret_snapshot = store.read_native_oauth("codex", home=tmp_path, allow_secret=True)
    assert secret_snapshot is not None
    assert secret_snapshot.keychain_edit is not None
    store.apply_keychain_edit(secret_snapshot.keychain_edit)

    remaining = json.loads(auth_path.read_text(encoding="utf-8"))
    assert remaining == {"unrelated": {"keep": "yes"}}


def test_codex_default_store_is_file_and_keyring_does_not_fallback_to_stale_file(
    tmp_path: Path,
    fake_keychain: FakeKeychain,
) -> None:
    auth_path = tmp_path / ".codex" / "auth.json"
    auth_path.parent.mkdir()
    auth_path.write_text(json.dumps(_codex_payload()), encoding="utf-8")

    default_snapshot = store.read_native_oauth("codex", home=tmp_path)
    assert default_snapshot is not None
    assert default_snapshot.payload == _codex_payload()

    (auth_path.parent / "config.toml").write_text(
        'cli_auth_credentials_store = "keyring"\n',
        encoding="utf-8",
    )
    keyring_snapshot = store.read_native_oauth("codex", home=tmp_path)
    assert keyring_snapshot is None
    assert fake_keychain.metadata_calls


@pytest.mark.parametrize("mode", ["auto", "keyring"])
def test_codex_keychain_metadata_scan_never_reads_secret(
    tmp_path: Path,
    fake_keychain: FakeKeychain,
    mode: str,
) -> None:
    codex_home = (tmp_path / ".codex").resolve()
    account = f"cli|{store._sha256(str(codex_home).encode('utf-8'))[:16]}"
    fake_keychain.items[("Codex Auth", account)] = (
        json.dumps(_codex_payload()),
        "native-v1",
    )
    (codex_home / "config.toml").parent.mkdir()
    (codex_home / "config.toml").write_text(
        f'cli_auth_credentials_store = "{mode}"\n',
        encoding="utf-8",
    )

    metadata_snapshot = store.read_native_oauth("codex", home=tmp_path)

    assert metadata_snapshot is not None
    assert metadata_snapshot.exportable is False
    assert metadata_snapshot.payload["status"] == "metadata_only"
    assert metadata_snapshot.revision == store._keychain_revision(
        "codex", "Codex Auth", account, "native-v1"
    )
    assert fake_keychain.read_calls == []

    secret_snapshot = store.read_native_oauth("codex", home=tmp_path, allow_secret=True)
    assert secret_snapshot is not None
    assert secret_snapshot.exportable is True
    assert fake_keychain.read_calls == [("Codex Auth", account)]
    assert secret_snapshot.revision == metadata_snapshot.revision


def test_denied_keychain_read_returns_permission_placeholder(
    tmp_path: Path,
    fake_keychain: FakeKeychain,
) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'cli_auth_credentials_store = "keyring"\n',
        encoding="utf-8",
    )
    fake_keychain.metadata_denied = True

    snapshot = store.read_native_oauth("codex", home=tmp_path)

    assert snapshot is not None
    assert snapshot.exportable is False
    assert snapshot.payload == {
        "status": "permission_needed",
        "store": "keychain",
        "service": "Codex Auth",
        "account": snapshot.payload["account"],
    }


def test_codex_secrets_backend_is_unsupported_and_does_not_use_stale_file(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(json.dumps(_codex_payload()), encoding="utf-8")
    (codex_home / "config.toml").write_text(
        'cli_auth_credentials_store = "auto"\nsecret_auth_storage = "managed"\n',
        encoding="utf-8",
    )

    snapshot = store.read_native_oauth("codex", home=tmp_path)

    assert snapshot is not None
    assert snapshot.exportable is False
    assert snapshot.payload == {"status": "unsupported_store", "store": "secret_auth_storage"}


def test_codex_ephemeral_ignores_existing_file(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(json.dumps(_codex_payload()), encoding="utf-8")
    (codex_home / "config.toml").write_text(
        'cli_auth_credentials_store = "ephemeral"\n',
        encoding="utf-8",
    )

    assert store.read_native_oauth("codex", home=tmp_path) is None


def test_claude_keychain_and_file_cleanup_preserve_unrelated_fields(
    tmp_path: Path,
    fake_keychain: FakeKeychain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USER", "test-user")
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    credentials_path = claude_home / ".credentials.json"
    credentials_path.write_text(json.dumps(_claude_payload()), encoding="utf-8")
    account = "test-user"
    fake_keychain.items[("Claude Code-credentials", account)] = (
        json.dumps(_claude_payload()),
        "claude-v1",
    )

    snapshot = store.read_native_oauth("claude", home=tmp_path, allow_secret=True)

    assert snapshot is not None
    assert snapshot.exportable is True
    assert snapshot.keychain_edit is not None
    assert len(snapshot.keychain_edit["operations"]) == 2

    store.apply_keychain_edit(snapshot.keychain_edit)

    keychain_after = json.loads(fake_keychain.items[("Claude Code-credentials", account)][0])
    file_after = json.loads(credentials_path.read_text(encoding="utf-8"))
    assert keychain_after == {"mcpOAuth": {"provider": "keep-me"}}
    assert file_after == {"mcpOAuth": {"provider": "keep-me"}}

    store.apply_keychain_edit(snapshot.keychain_edit, reverse=True)
    assert json.loads(fake_keychain.items[("Claude Code-credentials", account)][0]) == _claude_payload()
    assert json.loads(credentials_path.read_text(encoding="utf-8")) == _claude_payload()


def test_claude_locator_uses_secure_root_suffix_and_sanitized_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secure_root = (tmp_path / "secure-root").resolve()
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(secure_root))
    monkeypatch.setenv("USER", "bad/user")

    resolved_root, service, account = store._claude_locator(None)

    assert resolved_root == secure_root
    assert service == f"Claude Code-credentials-{store._sha256(str(secure_root).encode())[:8]}"
    assert account == "claude-code-user"


def test_claude_empty_secure_env_keeps_unsuffixed_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")
    monkeypatch.setenv("USER", "valid.user-1")

    _, service, account = store._claude_locator(None)

    assert service == "Claude Code-credentials"
    assert account == "valid.user-1"


def test_changed_keychain_revision_blocks_cleanup_without_mutation(
    tmp_path: Path,
    fake_keychain: FakeKeychain,
) -> None:
    codex_home = (tmp_path / ".codex").resolve()
    account = f"cli|{store._sha256(str(codex_home).encode('utf-8'))[:16]}"
    fake_keychain.items[("Codex Auth", account)] = (
        json.dumps(_codex_payload()),
        "native-v1",
    )
    (codex_home / "config.toml").parent.mkdir()
    (codex_home / "config.toml").write_text(
        'cli_auth_credentials_store = "keyring"\n',
        encoding="utf-8",
    )
    snapshot = store.read_native_oauth("codex", home=tmp_path, allow_secret=True)
    assert snapshot is not None and snapshot.keychain_edit is not None

    fake_keychain.items[("Codex Auth", account)] = (
        json.dumps({**_codex_payload(), "changed": True}),
        "native-v2",
    )
    with pytest.raises(store.NativeOAuthRevisionError):
        store.apply_keychain_edit(snapshot.keychain_edit)
    assert fake_keychain.delete_calls == []


def test_denied_keychain_delete_is_sanitized(
    tmp_path: Path,
    fake_keychain: FakeKeychain,
) -> None:
    codex_home = (tmp_path / ".codex").resolve()
    account = f"cli|{store._sha256(str(codex_home).encode('utf-8'))[:16]}"
    oauth_only = {
        "tokens": _codex_payload()["tokens"],
        "last_refresh": "2026-09-20T00:00:00Z",
    }
    fake_keychain.items[("Codex Auth", account)] = (
        json.dumps(oauth_only),
        "native-v1",
    )
    (codex_home / "config.toml").parent.mkdir()
    (codex_home / "config.toml").write_text(
        'cli_auth_credentials_store = "keyring"\n',
        encoding="utf-8",
    )
    snapshot = store.read_native_oauth("codex", home=tmp_path, allow_secret=True)
    assert snapshot is not None and snapshot.keychain_edit is not None
    fake_keychain.delete_denied = True

    with pytest.raises(store.NativeOAuthPermissionError) as error:
        store.apply_keychain_edit(snapshot.keychain_edit)
    assert "access-secret" not in str(error.value)
    assert "refresh-secret" not in str(error.value)


def test_apply_is_idempotent_after_keychain_cleanup(
    tmp_path: Path,
    fake_keychain: FakeKeychain,
) -> None:
    codex_home = (tmp_path / ".codex").resolve()
    account = f"cli|{store._sha256(str(codex_home).encode('utf-8'))[:16]}"
    oauth_only = {
        "tokens": _codex_payload()["tokens"],
        "last_refresh": "2026-09-20T00:00:00Z",
    }
    fake_keychain.items[("Codex Auth", account)] = (
        json.dumps(oauth_only),
        "native-v1",
    )
    (codex_home / "config.toml").parent.mkdir()
    (codex_home / "config.toml").write_text(
        'cli_auth_credentials_store = "keyring"\n',
        encoding="utf-8",
    )
    snapshot = store.read_native_oauth("codex", home=tmp_path, allow_secret=True)
    assert snapshot is not None and snapshot.keychain_edit is not None

    store.apply_keychain_edit(snapshot.keychain_edit)
    store.apply_keychain_edit(snapshot.keychain_edit)

    assert fake_keychain.delete_calls == [("Codex Auth", account)]


def test_security_runner_never_receives_secret_in_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(args: list[str], **kwargs):
        calls.append((args, kwargs))
        return store.subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(store, "subprocess", type("Subprocess", (), {
        "run": staticmethod(fake_run),
        "CompletedProcess": __import__("subprocess").CompletedProcess,
        "DEVNULL": __import__("subprocess").DEVNULL,
        "SubprocessError": __import__("subprocess").SubprocessError,
    }))

    secret = "refresh-secret"
    store._run_security(["add-generic-password", "-U", "-a", "account", "-s", "service", "-w"], input_text=secret)

    assert secret not in calls[0][0]
    assert calls[0][1]["input"] == secret
