from __future__ import annotations

import ctypes
import ctypes.util
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from vibe import native_oauth_store as store


class FakeKeychain:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], tuple[str, str]] = {}
        self.mdates: dict[tuple[str, str], float] = {}
        self.metadata_denied = False
        self.read_denied = False
        self.delete_denied = False
        self.fail_on_delete_accounts: set[str] = set()
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
        mdate = self.mdates.setdefault((service, account), 1.0)
        return store._KeychainMetadata(
            "found",
            item[1],
            {"acct": account, "svce": service, "mdat": mdate},
            f"fake:{service}:{account}",
            mdate,
        )

    def read(
        self,
        service: str,
        account: str,
        *,
        expected: store._KeychainRead | None = None,
    ) -> store._KeychainRead:
        self.read_calls.append((service, account))
        if self.read_denied:
            raise store._KeychainDenied("denied")
        item = self.items.get((service, account))
        if item is None:
            raise store._KeychainNotFound("missing")
        mdate = self.mdates.setdefault((service, account), 1.0)
        if expected is not None and (
            expected.persistent_reference != f"fake:{service}:{account}"
            or expected.modification_date != mdate
        ):
            raise store._KeychainNotFound("changed")
        return store._KeychainRead(
            item[0],
            item[1],
            f"fake:{service}:{account}",
            mdate,
        )

    def write(
        self,
        service: str,
        account: str,
        value: str,
        *,
        expected: store._KeychainRead | None = None,
    ) -> None:
        current = self.items.get((service, account))
        mdate = self.mdates.setdefault((service, account), 1.0)
        if expected is not None and (
            current is None
            or expected.persistent_reference != f"fake:{service}:{account}"
            or expected.modification_date != mdate
        ):
            raise store._KeychainNotFound("changed")
        self.write_calls.append((service, account, value))
        self.items[(service, account)] = (value, f"write-{len(self.write_calls)}")
        self.mdates[(service, account)] = mdate + 1

    def delete(
        self,
        service: str,
        account: str,
        *,
        expected: store._KeychainRead,
    ) -> None:
        self.delete_calls.append((service, account))
        if self.delete_denied or account in self.fail_on_delete_accounts:
            raise store._KeychainDenied("denied")
        current = self.items.get((service, account))
        mdate = self.mdates.setdefault((service, account), 1.0)
        if (
            current is None
            or expected.persistent_reference != f"fake:{service}:{account}"
            or expected.modification_date != mdate
        ):
            raise store._KeychainNotFound("changed")
        self.items.pop((service, account), None)
        self.mdates.pop((service, account), None)


class FakeCoreFoundation:
    def __init__(self) -> None:
        self.objects: dict[int, tuple[str, object]] = {}
        self.buffers: list[object] = []
        self.next_ref = 1

    def ref(self, kind: str, value: object) -> int:
        result = self.next_ref
        self.next_ref += 1
        self.objects[result] = (kind, value)
        return result

    @staticmethod
    def key(ref: int | ctypes.c_void_p) -> int:
        return int(ref.value) if isinstance(ref, ctypes.c_void_p) else ref

    def get(self, ref: int | ctypes.c_void_p) -> object:
        return self.objects[self.key(ref)][1]

    def kind(self, ref: int | ctypes.c_void_p) -> str:
        return self.objects[self.key(ref)][0]

    def CFRelease(self, _ref: int) -> None:
        return None

    def CFStringCreateWithBytes(
        self,
        _allocator,
        buffer,
        length,
        _encoding,
        _is_external_representation,
    ) -> int:
        return self.ref("string", bytes(buffer[:length]).decode("utf-8"))

    def CFStringGetTypeID(self) -> int:
        return 1

    def CFStringGetLength(self, ref: int) -> int:
        return len(self.get(ref))

    def CFStringGetMaximumSizeForEncoding(self, length: int, _encoding) -> int:
        return length * 4

    def CFStringGetCString(self, ref: int, buffer, _size, _encoding) -> bool:
        buffer.value = self.get(ref).encode("utf-8")
        return True

    def CFDataGetTypeID(self) -> int:
        return 2

    def CFDataGetLength(self, ref: int) -> int:
        return len(self.get(ref))

    def CFDataGetBytePtr(self, ref: int):
        raw = self.get(ref)
        buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        self.buffers.append(buffer)
        return buffer

    def CFDataCreate(self, _allocator, buffer, length) -> int:
        return self.ref("data", bytes(buffer[:length]))

    def CFDateCreate(self, _allocator, value) -> int:
        return self.ref("date", float(value))

    def CFDateGetTypeID(self) -> int:
        return 3

    def CFDateGetAbsoluteTime(self, ref: int) -> float:
        return float(self.get(ref))

    def CFNumberGetTypeID(self) -> int:
        return 4

    def CFNumberGetValue(self, *_args) -> bool:
        return False

    def CFBooleanGetTypeID(self) -> int:
        return 5

    def CFBooleanGetValue(self, ref: int) -> bool:
        return bool(self.get(ref))

    def CFGetTypeID(self, ref: int) -> int:
        return {
            "string": 1,
            "data": 2,
            "date": 3,
            "number": 4,
            "boolean": 5,
            "dictionary": 6,
            "array": 7,
        }[self.kind(ref)]

    def CFDictionaryCreateMutable(self, _allocator, _capacity, _keys, _values) -> int:
        return self.ref("dictionary", {})

    def CFDictionarySetValue(self, dictionary: int, key: int, value: int) -> None:
        self.get(dictionary)[key] = value

    def CFDictionaryGetValue(self, dictionary: int, key: int) -> int | None:
        return self.get(dictionary).get(key)

    def CFDictionaryGetTypeID(self) -> int:
        return 6

    def CFArrayCreate(self, _allocator, values, length, _callbacks) -> int:
        return self.ref("array", [self.key(values[index]) for index in range(length)])

    def CFArrayGetCount(self, array: int) -> int:
        return len(self.get(array))

    def CFArrayGetValueAtIndex(self, array: int, index: int) -> int:
        return self.get(array)[index]


class FakeSecurityBindings:
    def __init__(self) -> None:
        self.cf = FakeCoreFoundation()
        self.security = self
        self.key_callbacks = store._CFDictionaryKeyCallbacks()
        self.value_callbacks = store._CFDictionaryValueCallbacks()
        self.array_callbacks = store._CFArrayCallbacks()
        self.constants = {
            name: self.cf.ref("string", name)
            for name in (
                "kCFAllocatorDefault",
                "kCFBooleanFalse",
                "kCFBooleanTrue",
                "kSecAttrAccount",
                "kSecAttrCreationDate",
                "kSecAttrModificationDate",
                "kSecAttrPersistentReference",
                "kSecAttrService",
                "kSecClass",
                "kSecClassGenericPassword",
                "kSecMatchItemList",
                "kSecMatchLimit",
                "kSecMatchLimitOne",
                "kSecReturnAttributes",
                "kSecReturnData",
                "kSecReturnPersistentRef",
                "kSecUseAuthenticationUI",
                "kSecUseAuthenticationUIAllow",
                "kSecUseAuthenticationUIFail",
                "kSecValuePersistentRef",
                "kSecValueData",
            )
        }
        self.cf.objects[self.constants["kCFBooleanFalse"]] = ("boolean", False)
        self.cf.objects[self.constants["kCFBooleanTrue"]] = ("boolean", True)
        self.items: dict[tuple[str, str], dict[int, int]] = {}
        self.queries: list[int] = []
        self.interaction_allowed = True
        self.interaction_calls: list[bool] = []

    def constant(self, name: str) -> int:
        return self.constants[name]

    def add_item(self, service: str, account: str, value: str, mdat: float) -> None:
        attrs = {
            self.constant("kSecAttrService"): self.cf.ref("string", service),
            self.constant("kSecAttrAccount"): self.cf.ref("string", account),
            self.constant("kSecAttrCreationDate"): self.cf.ref("date", mdat - 1),
            self.constant("kSecAttrModificationDate"): self.cf.ref("date", mdat),
            self.constant("kSecValuePersistentRef"): self.cf.ref(
                "data",
                f"persistent:{service}:{account}".encode(),
            ),
            self.constant("kSecValueData"): self.cf.ref("data", value.encode()),
        }
        self.items[(service, account)] = attrs

    def _locator(self, query: int) -> tuple[str, str]:
        values = self.cf.get(query)
        service = self.cf.get(values[self.constant("kSecAttrService")])
        account = self.cf.get(values[self.constant("kSecAttrAccount")])
        item = self.items.get((service, account))
        item_list = values.get(self.constant("kSecMatchItemList"))
        if item_list is not None:
            references = self.cf.get(item_list)
            item_persistent = (
                item.get(self.constant("kSecValuePersistentRef")) if item else None
            )
            if (
                item is None
                or not references
                or item_persistent is None
                or self.cf.get(item_persistent) != self.cf.get(references[0])
            ):
                return service, f"{account}:stale"
        expected_date = values.get(self.constant("kSecAttrModificationDate"))
        if expected_date is not None and (
            item is None
            or self.cf.get(item[self.constant("kSecAttrModificationDate")])
            != self.cf.get(expected_date)
        ):
            return service, f"{account}:stale"
        return service, account

    def SecItemCopyMatching(self, query: int, output) -> int:
        self.queries.append(query)
        item = self.items.get(self._locator(query))
        if item is None:
            return -25300
        query_values = self.cf.get(query)
        result_values = {
            key: value
            for key, value in item.items()
            if key != self.constant("kSecValueData")
        }
        return_data = query_values.get(self.constant("kSecReturnData"))
        if return_data is not None and self.cf.get(return_data):
            result_values[self.constant("kSecValueData")] = item[
                self.constant("kSecValueData")
            ]
        result = self.cf.ref("dictionary", result_values)
        output._obj.value = result
        return 0

    def SecItemUpdate(self, query: int, attributes: int) -> int:
        item = self.items.get(self._locator(query))
        if item is None:
            return -25300
        item[self.constant("kSecValueData")] = self.cf.get(attributes)[
            self.constant("kSecValueData")
        ]
        current = self.cf.get(item[self.constant("kSecAttrModificationDate")])
        item[self.constant("kSecAttrModificationDate")] = self.cf.ref("date", current + 1)
        return 0

    def SecItemAdd(self, item: int, _result) -> int:
        values = self.cf.get(item)
        service = self.cf.get(values[self.constant("kSecAttrService")])
        account = self.cf.get(values[self.constant("kSecAttrAccount")])
        if (service, account) in self.items:
            return -25299
        now = 20.0 + len(self.items)
        values = dict(values)
        values.setdefault(
            self.constant("kSecAttrCreationDate"),
            self.cf.ref("date", now),
        )
        values.setdefault(
            self.constant("kSecAttrModificationDate"),
            self.cf.ref("date", now),
        )
        values.setdefault(
            self.constant("kSecValuePersistentRef"),
            self.cf.ref("data", f"persistent:{service}:{account}".encode()),
        )
        self.items[(service, account)] = values
        return 0

    def SecItemDelete(self, query: int) -> int:
        locator = self._locator(query)
        if locator not in self.items:
            return -25300
        self.items.pop(locator)
        return 0

    def SecKeychainGetUserInteractionAllowed(self, output) -> int:
        self.interaction_calls.append(True)
        output._obj.value = self.interaction_allowed
        return 0

    def SecKeychainSetUserInteractionAllowed(self, value) -> int:
        self.interaction_calls.append(bool(value))
        self.interaction_allowed = bool(value)
        return 0


@pytest.fixture
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeychain:
    fake = FakeKeychain()
    monkeypatch.setattr(store, "_KEYCHAIN_STORE", fake)
    return fake


@pytest.fixture
def fake_security_bindings(monkeypatch: pytest.MonkeyPatch) -> FakeSecurityBindings:
    fake = FakeSecurityBindings()
    monkeypatch.setattr(store, "_security_bindings", lambda: fake)
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
            "accountUuid": "account-1",
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


def test_fixture_home_never_selects_real_keychain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'cli_auth_credentials_store = "keyring"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(store, "_KEYCHAIN_STORE", store._DEFAULT_KEYCHAIN_STORE)
    monkeypatch.setattr(
        store,
        "_security_bindings",
        lambda: pytest.fail("fixture home selected the real Keychain"),
    )

    assert store.read_native_oauth("codex", home=tmp_path) is None


def test_secret_keychain_read_denial_is_permission_placeholder(
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
    metadata = store.read_native_oauth("codex", home=tmp_path)
    fake_keychain.read_denied = True

    secret = store.read_native_oauth("codex", home=tmp_path, allow_secret=True)

    assert metadata is not None
    assert secret is not None
    assert secret.exportable is False
    assert secret.revision == metadata.revision
    assert secret.payload["status"] == "permission_needed"


def test_secret_read_returns_changed_placeholder_when_scan_revision_is_stale(
    tmp_path: Path,
    fake_keychain: FakeKeychain,
    monkeypatch: pytest.MonkeyPatch,
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
    original_metadata = fake_keychain.metadata
    metadata_values: list[store._KeychainMetadata] = []

    def metadata_then_change(service: str, item_account: str) -> store._KeychainMetadata:
        metadata_value = original_metadata(service, item_account)
        metadata_values.append(metadata_value)
        fake_keychain.items[(service, item_account)] = (
            json.dumps(_codex_payload()),
            "native-v2",
        )
        return metadata_value

    monkeypatch.setattr(fake_keychain, "metadata", metadata_then_change)

    secret = store.read_native_oauth("codex", home=tmp_path, allow_secret=True)

    assert secret is not None
    assert secret.exportable is False
    assert secret.revision != store._keychain_revision(
        "codex",
        "Codex Auth",
        account,
        metadata_values[0].revision,
    )
    assert secret.payload["status"] == "changed"


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


def test_security_framework_keychain_uses_metadata_without_data_and_tracks_mdat(
    fake_security_bindings: FakeSecurityBindings,
) -> None:
    fake_security_bindings.add_item(
        "Codex Auth",
        "account",
        json.dumps(_codex_payload()),
        mdat=10.0,
    )
    native = store._SecurityKeychainStore()

    metadata = native.metadata("Codex Auth", "account")
    read = native.read("Codex Auth", "account")

    assert metadata.state == "found"
    assert metadata.attributes["mdat"] == 10.0
    assert metadata.persistent_reference is not None
    assert read.value == json.dumps(_codex_payload())
    assert read.revision == metadata.revision
    assert read.persistent_reference == metadata.persistent_reference
    metadata_query = fake_security_bindings.cf.get(fake_security_bindings.queries[0])
    read_query = fake_security_bindings.cf.get(fake_security_bindings.queries[1])
    assert fake_security_bindings.cf.get(
        metadata_query[fake_security_bindings.constant("kSecReturnAttributes")]
    )
    assert fake_security_bindings.constant("kSecReturnData") not in metadata_query
    assert (
        fake_security_bindings.cf.get(
            metadata_query[fake_security_bindings.constant("kSecUseAuthenticationUI")]
        )
        == fake_security_bindings.cf.get(
            fake_security_bindings.constant("kSecUseAuthenticationUIFail")
        )
    )
    assert (
        fake_security_bindings.cf.get(
            read_query[fake_security_bindings.constant("kSecUseAuthenticationUI")]
        )
        == fake_security_bindings.cf.get(
            fake_security_bindings.constant("kSecUseAuthenticationUIAllow")
        )
    )
    assert fake_security_bindings.interaction_calls == [True, False, True]
    assert fake_security_bindings.cf.get(
        read_query[fake_security_bindings.constant("kSecReturnData")]
    )

    fake_security_bindings.items[("Codex Auth", "account")][
        fake_security_bindings.constant("kSecAttrModificationDate")
    ] = fake_security_bindings.cf.ref("date", 11.0)
    changed = native.metadata("Codex Auth", "account")
    assert changed.revision != metadata.revision


def test_security_framework_update_preserves_unrelated_attributes_and_delete(
    fake_security_bindings: FakeSecurityBindings,
) -> None:
    fake_security_bindings.add_item("Codex Auth", "account", "before", mdat=10.0)
    unrelated_key = fake_security_bindings.cf.ref("string", "unrelated")
    unrelated_value = fake_security_bindings.cf.ref("string", "preserve")
    fake_security_bindings.items[("Codex Auth", "account")][unrelated_key] = unrelated_value
    native = store._SecurityKeychainStore()

    metadata = native.metadata("Codex Auth", "account")
    assert metadata.persistent_reference is not None
    native.write(
        "Codex Auth",
        "account",
        "after",
        expected=native.read("Codex Auth", "account"),
    )
    update_query = fake_security_bindings.cf.get(fake_security_bindings.queries[-1])
    assert (
        fake_security_bindings.cf.get(
            update_query[fake_security_bindings.constant("kSecUseAuthenticationUI")]
        )
        == fake_security_bindings.cf.get(
            fake_security_bindings.constant("kSecUseAuthenticationUIAllow")
        )
    )

    item = fake_security_bindings.items[("Codex Auth", "account")]
    assert fake_security_bindings.cf.get(item[unrelated_key]) == "preserve"
    assert fake_security_bindings.cf.get(item[fake_security_bindings.constant("kSecValueData")]) == (
        b"after"
    )
    native.delete(
        "Codex Auth",
        "account",
        expected=native.read("Codex Auth", "account"),
    )
    delete_query = fake_security_bindings.cf.get(fake_security_bindings.queries[-1])
    assert (
        fake_security_bindings.cf.get(
            delete_query[fake_security_bindings.constant("kSecUseAuthenticationUI")]
        )
        == fake_security_bindings.cf.get(
            fake_security_bindings.constant("kSecUseAuthenticationUIAllow")
        )
    )
    assert ("Codex Auth", "account") not in fake_security_bindings.items


def test_security_framework_reverse_restore_after_delete_is_idempotent(
    fake_security_bindings: FakeSecurityBindings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_security_bindings.add_item("Codex Auth", "account", "before", mdat=10.0)
    native = store._SecurityKeychainStore()
    monkeypatch.setattr(store, "_KEYCHAIN_STORE", native)
    metadata = native.metadata("Codex Auth", "account")
    assert metadata.persistent_reference is not None
    edit = {
        "version": 1,
        "operations": [
            {
                "kind": "keychain",
                "service": "Codex Auth",
                "account": "account",
                "persistent_reference": metadata.persistent_reference,
                "modification_date": 10.0,
                "before": {
                    "exists": True,
                    "value": "before",
                    "revision": metadata.revision,
                },
                "after": {"exists": False},
            }
        ],
    }

    store.apply_keychain_edit(edit)
    assert ("Codex Auth", "account") not in fake_security_bindings.items
    store.apply_keychain_edit(edit, reverse=True)
    store.apply_keychain_edit(edit, reverse=True)
    assert (
        fake_security_bindings.cf.get(
            fake_security_bindings.items[("Codex Auth", "account")][
                fake_security_bindings.constant("kSecValueData")
            ]
        )
        == b"before"
    )


def test_apply_rechecks_each_operation_immediately_before_mutation(
    fake_keychain: FakeKeychain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_keychain.items[("service", "first")] = ("first", "v1")
    fake_keychain.items[("service", "second")] = ("second", "v2")
    edit = {
        "version": 1,
        "operations": [
            {
                "kind": "keychain",
                "service": "service",
                "account": "first",
                "before": {"exists": True, "value": "first", "revision": "v1"},
                "after": {"exists": True, "value": "first-new"},
            },
            {
                "kind": "keychain",
                "service": "service",
                "account": "second",
                "before": {"exists": True, "value": "second", "revision": "v2"},
                "after": {"exists": True, "value": "second-new"},
            },
        ],
    }
    original_live = store._live_keychain_state
    calls = 0

    def live_with_concurrent_login(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            fake_keychain.items[("service", "second")] = ("new-login", "v2-new")
        return original_live(*args, **kwargs)

    monkeypatch.setattr(store, "_live_keychain_state", live_with_concurrent_login)

    with pytest.raises(store.NativeOAuthRevisionError):
        store.apply_keychain_edit(edit)

    assert fake_keychain.items[("service", "first")][0] == "first-new"
    assert fake_keychain.items[("service", "second")] == ("new-login", "v2-new")


@pytest.mark.skipif(sys.platform != "darwin", reason="CoreFoundation smoke requires macOS")
def test_core_foundation_ffi_smoke_without_security_item_access() -> None:
    foundation_path = ctypes.util.find_library("CoreFoundation") or (
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    cf = ctypes.CDLL(foundation_path)
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None
    cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
    cf.CFGetTypeID.restype = ctypes.c_ulong
    cf.CFStringCreateWithBytes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_long,
        ctypes.c_uint32,
        ctypes.c_bool,
    ]
    cf.CFStringCreateWithBytes.restype = ctypes.c_void_p
    cf.CFStringGetTypeID.argtypes = []
    cf.CFStringGetTypeID.restype = ctypes.c_ulong
    cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
    cf.CFStringGetLength.restype = ctypes.c_long
    cf.CFStringGetMaximumSizeForEncoding.argtypes = [ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
    cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_uint32,
    ]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFDataCreate.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_long,
    ]
    cf.CFDataCreate.restype = ctypes.c_void_p
    cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
    cf.CFDataGetLength.restype = ctypes.c_long
    cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    cf.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_ubyte)
    cf.CFDateCreate.argtypes = [ctypes.c_void_p, ctypes.c_double]
    cf.CFDateCreate.restype = ctypes.c_void_p
    cf.CFArrayCreate.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_long,
        ctypes.POINTER(store._CFArrayCallbacks),
    ]
    cf.CFArrayCreate.restype = ctypes.c_void_p
    cf.CFDictionaryCreateMutable.argtypes = [
        ctypes.c_void_p,
        ctypes.c_long,
        ctypes.POINTER(store._CFDictionaryKeyCallbacks),
        ctypes.POINTER(store._CFDictionaryValueCallbacks),
    ]
    cf.CFDictionaryCreateMutable.restype = ctypes.c_void_p
    cf.CFDictionarySetValue.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    cf.CFDictionarySetValue.restype = None
    cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFDictionaryGetValue.restype = ctypes.c_void_p

    constants = {
        "kCFAllocatorDefault": ctypes.c_void_p.in_dll(cf, "kCFAllocatorDefault").value,
        "kCFBooleanFalse": ctypes.c_void_p.in_dll(cf, "kCFBooleanFalse").value,
        "kCFBooleanTrue": ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue").value,
    }
    bindings = SimpleNamespace(
        cf=cf,
        constants=constants,
        key_callbacks=store._CFDictionaryKeyCallbacks.in_dll(
            cf,
            "kCFTypeDictionaryKeyCallBacks",
        ),
        value_callbacks=store._CFDictionaryValueCallbacks.in_dll(
            cf,
            "kCFTypeDictionaryValueCallBacks",
        ),
        array_callbacks=store._CFArrayCallbacks.in_dll(
            cf,
            "kCFTypeArrayCallBacks",
        ),
        constant=lambda name: constants[name],
    )
    assert not hasattr(bindings, "security")
    security_constant_names = (
        "kSecAttrAccount",
        "kSecAttrCreationDate",
            "kSecAttrModificationDate",
            "kSecAttrPersistentReference",
            "kSecAttrService",
            "kSecClass",
            "kSecClassGenericPassword",
            "kSecMatchItemList",
            "kSecMatchLimit",
        "kSecMatchLimitOne",
        "kSecReturnAttributes",
        "kSecReturnData",
        "kSecReturnPersistentRef",
        "kSecUseAuthenticationUI",
        "kSecUseAuthenticationUIAllow",
        "kSecUseAuthenticationUIFail",
        "kSecValuePersistentRef",
        "kSecValueData",
    )
    for name in security_constant_names:
        constants[name] = store._cf_string(bindings, name)
    unicode_ref = store._cf_string(bindings, "服务🚀")
    data_ref = store._cf_data(bindings, "opaque-test-data")
    key_ref = store._cf_string(bindings, "key")
    query, owned = store._keychain_query(
        bindings,
        "服务",
        "账户",
        return_attributes=True,
        return_data=False,
    )
    interactive_query, interactive_owned = store._keychain_query(
        bindings,
        "服务",
        "账户",
        return_attributes=False,
        return_data=True,
        allow_interaction=True,
    )
    dictionary = store._cf_dictionary(bindings, {key_ref: unicode_ref})
    bound_query = None
    bound_owned: list[int] = []
    try:
        found = cf.CFDictionaryGetValue(dictionary, key_ref)
        assert store._cf_string_value(bindings, found) == "服务🚀"
        found_query_service = cf.CFDictionaryGetValue(
            query,
            constants["kSecAttrService"],
        )
        assert store._cf_string_value(bindings, found_query_service) == "服务"
        query_values = cf.CFDictionaryGetValue(
            query,
            constants["kSecUseAuthenticationUI"],
        )
        assert query_values == constants["kSecUseAuthenticationUIFail"]
        interactive_values = cf.CFDictionaryGetValue(
            interactive_query,
            constants["kSecUseAuthenticationUI"],
        )
        assert interactive_values == constants["kSecUseAuthenticationUIAllow"]
        data_length = cf.CFDataGetLength(data_ref)
        data_pointer = cf.CFDataGetBytePtr(data_ref)
        assert bytes(data_pointer[:data_length]) == b"opaque-test-data"
        observed = store._KeychainRead(
            value="",
            revision="",
            persistent_reference=store._persistent_reference_from_cf_data(
                bindings,
                data_ref,
            ),
            modification_date=123.0,
        )
        bound_query, bound_owned = store._keychain_query(
            bindings,
            "服务",
            "账户",
            return_attributes=False,
            return_data=False,
            expected=observed,
        )
        assert cf.CFDictionaryGetValue(
            bound_query,
            constants["kSecMatchItemList"],
        )
        assert cf.CFDictionaryGetValue(
            bound_query,
            constants["kSecAttrModificationDate"],
        )
    finally:
        store._release(
            bindings,
            dictionary,
            query,
            interactive_query,
            bound_query,
            unicode_ref,
            data_ref,
            key_ref,
            *owned,
            *interactive_owned,
            *bound_owned,
            *[constants[name] for name in security_constant_names],
        )


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


def test_codex_malformed_config_fails_closed_without_stale_file_fallback(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(json.dumps(_codex_payload()), encoding="utf-8")
    (codex_home / "config.toml").write_text("cli_auth_credentials_store = [\n", encoding="utf-8")

    snapshot = store.read_native_oauth("codex", home=tmp_path)

    assert snapshot is not None
    assert snapshot.exportable is False
    assert snapshot.payload == {
        "status": "unsupported_store",
        "store": "invalid_config",
    }


def test_codex_keychain_api_key_is_exportable_and_removed_without_losing_unrelated_fields(
    tmp_path: Path,
    fake_keychain: FakeKeychain,
) -> None:
    codex_home = (tmp_path / ".codex").resolve()
    account = f"cli|{store._sha256(str(codex_home).encode('utf-8'))[:16]}"
    api_only = {
        "OPENAI_API_KEY": "sk-secret",
        "unrelated": {"keep": "yes"},
    }
    fake_keychain.items[("Codex Auth", account)] = (
        json.dumps(api_only),
        "native-api-v1",
    )
    (codex_home / "config.toml").parent.mkdir()
    (codex_home / "config.toml").write_text(
        'cli_auth_credentials_store = "keyring"\n',
        encoding="utf-8",
    )

    metadata = store.read_native_oauth("codex", home=tmp_path)
    secret = store.read_native_oauth("codex", home=tmp_path, allow_secret=True)

    assert metadata is not None
    assert metadata.exportable is False
    assert secret is not None
    assert secret.exportable is True
    assert secret.payload == api_only
    assert secret.revision == metadata.revision
    assert secret.keychain_edit is not None
    store.apply_keychain_edit(secret.keychain_edit)
    assert json.loads(fake_keychain.items[("Codex Auth", account)][0]) == {
        "unrelated": {"keep": "yes"},
    }


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
    store.apply_keychain_edit(snapshot.keychain_edit)

    keychain_after = json.loads(fake_keychain.items[("Claude Code-credentials", account)][0])
    file_after = json.loads(credentials_path.read_text(encoding="utf-8"))
    assert keychain_after == {"mcpOAuth": {"provider": "keep-me"}}
    assert file_after == {"mcpOAuth": {"provider": "keep-me"}}

    store.apply_keychain_edit(snapshot.keychain_edit, reverse=True)
    store.apply_keychain_edit(snapshot.keychain_edit, reverse=True)
    assert json.loads(fake_keychain.items[("Claude Code-credentials", account)][0]) == _claude_payload()
    assert json.loads(credentials_path.read_text(encoding="utf-8")) == _claude_payload()


def test_claude_locator_uses_secure_root_suffix_and_sanitized_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secure_root = (tmp_path / "secure-root").resolve()
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(secure_root))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("USER", "bad/user")

    resolved_root, service, account = store._claude_locator(None)

    assert resolved_root == secure_root
    assert service == "Claude Code-credentials"
    assert account == "claude-code-user"


def test_claude_empty_secure_env_keeps_unsuffixed_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")
    monkeypatch.setenv("USER", "valid.user-1")

    _, service, account = store._claude_locator(None)

    assert service == "Claude Code-credentials"
    assert account == "valid.user-1"


def test_claude_no_config_env_keeps_unsuffixed_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    _, service, _ = store._claude_locator(None)

    assert service == "Claude Code-credentials"


def test_claude_config_env_adds_suffix_when_secure_env_is_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_root = (tmp_path / "config-root").resolve()
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_root))

    resolved_root, service, _ = store._claude_locator(None)

    assert resolved_root == config_root
    assert service == f"Claude Code-credentials-{store._sha256(str(config_root).encode())[:8]}"


def test_claude_nonempty_secure_env_hashes_when_config_env_is_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secure_root = (tmp_path / "secure-root").resolve()
    config_root = (tmp_path / "config-root").resolve()
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(secure_root))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_root))

    resolved_root, service, _ = store._claude_locator(None)

    assert resolved_root == secure_root
    assert service == f"Claude Code-credentials-{store._sha256(str(secure_root).encode())[:8]}"


def test_claude_empty_secure_env_suppresses_suffix_even_when_config_is_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_root = (tmp_path / "config-root").resolve()
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_root))

    _, service, _ = store._claude_locator(None)

    assert service == "Claude Code-credentials"


def test_claude_keychain_and_different_fallback_account_are_not_exportable(
    tmp_path: Path,
    fake_keychain: FakeKeychain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USER", "test-user")
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    credentials_path = claude_home / ".credentials.json"
    fallback = _claude_payload()
    fallback["claudeAiOauth"]["accountUuid"] = "different-account"
    credentials_path.write_text(json.dumps(fallback), encoding="utf-8")
    fake_keychain.items[("Claude Code-credentials", "test-user")] = (
        json.dumps(_claude_payload()),
        "claude-v1",
    )

    snapshot = store.read_native_oauth("claude", home=tmp_path, allow_secret=True)

    assert snapshot is not None
    assert snapshot.exportable is False
    assert snapshot.keychain_edit is None
    assert snapshot.payload == {
        "status": "conflict",
        "store": "keychain+file",
        "service": "Claude Code-credentials",
        "account": "test-user",
    }
    assert json.loads(credentials_path.read_text(encoding="utf-8")) == fallback


def test_claude_keychain_and_fallback_same_refresh_grant_without_identity_are_exportable(
    tmp_path: Path,
    fake_keychain: FakeKeychain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USER", "test-user")
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    keychain_payload = _claude_payload()
    fallback_payload = _claude_payload()
    keychain_payload["claudeAiOauth"].pop("accountUuid")
    fallback_payload["claudeAiOauth"].pop("accountUuid")
    credentials_path = claude_home / ".credentials.json"
    credentials_path.write_text(json.dumps(fallback_payload), encoding="utf-8")
    fake_keychain.items[("Claude Code-credentials", "test-user")] = (
        json.dumps(keychain_payload),
        "claude-v1",
    )

    snapshot = store.read_native_oauth("claude", home=tmp_path, allow_secret=True)

    assert snapshot is not None
    assert snapshot.exportable is True
    assert snapshot.keychain_edit is not None
    assert len(snapshot.keychain_edit["operations"]) == 2


def test_claude_keychain_and_different_refresh_grants_without_identity_conflict(
    tmp_path: Path,
    fake_keychain: FakeKeychain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USER", "test-user")
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    keychain_payload = _claude_payload()
    fallback_payload = _claude_payload()
    keychain_payload["claudeAiOauth"].pop("accountUuid")
    fallback_payload["claudeAiOauth"].pop("accountUuid")
    fallback_payload["claudeAiOauth"]["refreshToken"] = "different-refresh"
    credentials_path = claude_home / ".credentials.json"
    credentials_path.write_text(json.dumps(fallback_payload), encoding="utf-8")
    fake_keychain.items[("Claude Code-credentials", "test-user")] = (
        json.dumps(keychain_payload),
        "claude-v1",
    )

    snapshot = store.read_native_oauth("claude", home=tmp_path, allow_secret=True)

    assert snapshot is not None
    assert snapshot.exportable is False
    assert snapshot.payload["status"] == "conflict"


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


def test_check_keychain_edit_exposes_before_and_after_cas_checks(
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

    assert store.check_keychain_edit(snapshot.keychain_edit) is True
    store.apply_keychain_edit(snapshot.keychain_edit)
    assert store.check_keychain_edit(snapshot.keychain_edit, reverse=True) is True
    assert store.check_keychain_edit(snapshot.keychain_edit, applied=True) is True
    store.apply_keychain_edit(snapshot.keychain_edit, reverse=True)
    assert store.check_keychain_edit(
        snapshot.keychain_edit,
        applied=True,
        reverse=True,
    ) is True


def test_apply_does_not_rollback_prior_operations_when_a_later_one_fails(
    fake_keychain: FakeKeychain,
) -> None:
    fake_keychain.items[("service", "first")] = ("first", "v1")
    fake_keychain.items[("service", "second")] = ("second", "v2")
    fake_keychain.fail_on_delete_accounts.add("second")
    edit = {
        "version": 1,
        "operations": [
            {
                "kind": "keychain",
                "service": "service",
                "account": "first",
                "before": {"exists": True, "value": "first", "revision": "v1"},
                "after": {"exists": False},
            },
            {
                "kind": "keychain",
                "service": "service",
                "account": "second",
                "before": {"exists": True, "value": "second", "revision": "v2"},
                "after": {"exists": False},
            },
        ],
    }

    with pytest.raises(store.NativeOAuthPermissionError):
        store.apply_keychain_edit(edit)

    assert ("service", "first") not in fake_keychain.items
    assert ("service", "second") in fake_keychain.items


def test_file_secret_snapshot_uses_the_original_read_for_before_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth_path = tmp_path / ".codex" / "auth.json"
    auth_path.parent.mkdir()
    auth_path.write_text(json.dumps(_codex_payload()), encoding="utf-8")
    monkeypatch.setattr(
        store,
        "_json_file_state",
        lambda _path: pytest.fail("file snapshot reread"),
    )

    snapshot = store.read_native_oauth("codex", home=tmp_path, allow_secret=True)

    assert snapshot is not None and snapshot.keychain_edit is not None


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


def test_keychain_backend_has_no_security_cli_secret_path() -> None:
    assert not hasattr(store, "_run_security")
    assert "security" not in store.__dict__
