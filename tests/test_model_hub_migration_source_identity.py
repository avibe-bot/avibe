"""Takeover identity is a credential target, not a file path or placement."""

import asyncio
import json

import pytest

from config.v2_config import (
    ModelHubBackendModelConfig,
    ModelHubModelConfig,
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
)
from core.handlers.model_hub.migration import scan_native_configs
from core.handlers.model_hub.migration_files import plan_native_cleanup
from core.handlers.model_hub.migration_journal import NativeFileEdit, TakeoverStateError
from core.handlers.model_hub.service import ModelHubError
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import (
    _isolate_native_home,
    _service,
    _write,
    _write_claude_oauth,
    _write_codex_oauth,
)


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("placement", ["absent", "default", "manual"])
def test_reused_credential_remains_available_to_consented_backend(
    monkeypatch, tmp_path, backend, placement,
):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    service, store, adapter = _service(tmp_path, migration_home=home)
    vendor = "anthropic" if backend == "claude" else "openai"
    for key in ("fixture-selected", "fixture-unrelated"):
        asyncio.run(service.create_source({
            "kind": "api_key", "vendor": vendor, "key": key,
            "display_name": key,
        }))
    selected, unrelated = store.config.sources
    agent = store.config.agents[backend]
    agent.mode = "direct"
    if placement != "default":
        agent.sources.order.remove(selected.id)
    # Existing explicit intent must not be rewritten by default placement.
    if backend == "opencode":
        agent.models = [ModelHubBackendModelConfig(
            id="fixture/model", origin="manual", native_protocol="openai_responses",
        )]
    menu_model = agent.models[0].id
    agent.routes[menu_model] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(
            selected.id if placement == "manual" else unrelated.id, "manual-target",
        ),),
    )
    if backend == "claude":
        native = home / ".claude/settings.json"
        content = {"env": {"ANTHROPIC_API_KEY": "fixture-selected"}}
    elif backend == "codex":
        native = home / ".codex/auth.json"
        content = {"OPENAI_API_KEY": "fixture-selected"}
    else:
        native = home / ".config/opencode/opencode.json"
        content = {"provider": {"openai": {"options": {"apiKey": "fixture-selected"}}}}
    _write(native, json.dumps(content))
    store.config = service._clone_config(store.config)
    before = store.config.to_payload()
    ids = [row["id"] for row in service.migration_scan()["items"]]

    assert asyncio.run(service.migration_apply(ids))["applied"] == 1

    after = store.config.to_payload()
    assert after["sources"] == before["sources"]
    assert len(adapter.provisioned) == 2
    assert after["agents"][backend]["routes"] == before["agents"][backend]["routes"]
    expected = before["agents"][backend]["sources"]["order"]
    if placement == "absent":
        expected = [*expected, selected.id]
    assert store.config.effective_source_order(backend) == expected
    for other in {"claude", "codex", "opencode"} - {backend}:
        assert after["agents"][other] == before["agents"][other]
    assert store.config.agents[backend].mode == "hub"
    assert not native.exists() or "fixture-selected" not in native.read_text()
    assert service.migration_journal.load() is None


def _shared_opencode(monkeypatch, tmp_path, *, different_target=False, shadow_keys=False):
    home, project = tmp_path / "native", tmp_path / "project"
    _isolate_native_home(monkeypatch, home)
    paths = [
        home / ".config/opencode/opencode.json",
        project / ".opencode/opencode.json",
    ]
    for index, path in enumerate(paths):
        options = {"baseURL": "https://fixture.example/v1"}
        if different_target and index:
            options["baseURL"] = "https://different.example/v1"
        if shadow_keys:
            options["apiKey"] = f"fixture-shadow-{index}"
        _write(path, json.dumps({
            "provider": {"openai": {
                "options": options,
                "models": {f"manual-{index}": {"name": f"模型 {index}"}},
                "name": "保留",
            }},
            "mcp": {"unrelated": True},
        }))
    auth = home / ".local/share/opencode/auth.json"
    _write(auth, json.dumps({"openai": {"type": "api", "key": "fixture-shared"}}))
    service, store, adapter = _service(tmp_path, migration_home=home)
    service.migration_project_roots = lambda: (project,)
    return service, store, adapter, paths, auth


@pytest.mark.parametrize("different_target,shadow_keys,expected", [
    (False, False, 1),
    (True, False, 2),
    (False, True, 3),
])
def test_shared_auth_is_one_source_per_credential_target_not_config_path(
    monkeypatch, tmp_path, different_target, shadow_keys, expected,
):
    service, store, adapter, paths, auth = _shared_opencode(
        monkeypatch, tmp_path, different_target=different_target, shadow_keys=shadow_keys,
    )
    rows = service.migration_scan()["items"]
    assert len(rows) == expected
    assert rows == service.migration_scan()["items"]
    assert asyncio.run(service.migration_apply([row["id"] for row in rows]))["applied"] == expected
    assert len(adapter.provisioned) == expected
    assert len(store.config.sources) == expected
    assert len(store.config.agents["opencode"].sources.order) == expected
    shared = [source for source in store.config.sources
              if adapter.keys[source.credential_ref][2] == "fixture-shared"]
    assert len(shared) == (2 if different_target else 1)
    if not different_target:
        assert {
            (model.id, model.display_name)
            for model in shared[0].models if model.provenance == "manual"
        } == {("manual-0", "模型 0"), ("manual-1", "模型 1")}
    for path in paths:
        result = json.loads(path.read_text())
        assert result["provider"]["openai"]["name"] == "保留"
        assert "options" not in result["provider"]["openai"]
        assert result["mcp"] == {"unrelated": True}
    assert json.loads(auth.read_text()) == {}
    assert service.migration_scan()["items"] == []
    assert asyncio.run(service.migration_apply([row["id"] for row in rows]))["applied"] == expected
    assert len(adapter.provisioned) == expected


@pytest.mark.parametrize("change", ["model", "target", "key"])
def test_shared_credential_consent_binds_every_contributing_config(monkeypatch, tmp_path, change):
    service, _, adapter, paths, auth = _shared_opencode(monkeypatch, tmp_path)
    ids = [row["id"] for row in service.migration_scan()["items"]]
    assert len(ids) == 1
    payload = json.loads(paths[1].read_text())
    provider = payload["provider"]["openai"]
    if change == "model":
        provider["models"]["manual-new"] = {"name": "新模型"}
    elif change == "target":
        provider["options"]["baseURL"] = "https://new.example/v1"
    else:
        provider["options"]["apiKey"] = "fixture-new-login"
    paths[1].write_text(json.dumps(payload))
    before = {path: path.read_bytes() for path in [*paths, auth]}
    with pytest.raises(ModelHubError) as error:
        asyncio.run(service.migration_apply(ids))
    assert error.value.code == "migration_item_conflict"
    assert not adapter.provisioned
    assert {path: path.read_bytes() for path in before} == before
    assert service.migration_journal.load() is None


@pytest.mark.parametrize("backend,vendor,protocol,writer", [
    ("claude", "anthropic", "anthropic", _write_claude_oauth),
    ("codex", "openai", "openai_responses", _write_codex_oauth),
])
def test_existing_native_source_conversion_also_restores_missing_placement(
    monkeypatch, tmp_path, backend, vendor, protocol, writer,
):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    writer(home)
    service, store, adapter = _service(tmp_path, migration_home=home)
    store.config.sources.append(ModelHubSourceConfig(
        id="src_existingnative", kind="subscription", vendor=vendor,
        protocol=protocol, display_name="既有订阅", supply_channel="native_cli",
        billing="monthly", state=ModelHubSourceStateConfig(status="standby"),
        models=[ModelHubModelConfig(id="fixture-existing", provenance="manual", display_name="保留模型")],
    ))
    store.config.agents[backend].mode = "direct"
    before = service._clone_config(store.config).to_payload()
    ids = [row["id"] for row in service.migration_scan()["items"]]

    assert asyncio.run(service.migration_apply(ids))["applied"] == 1

    source, = store.config.sources
    assert source.id == "src_existingnative"
    assert source.supply_channel == "hub"
    assert [model.to_payload() for model in source.models] == before["sources"][0]["models"]
    assert store.config.effective_source_order(backend) == [source.id]
    assert len(adapter.oauth_provisioned) == 1
    for other in {"claude", "codex", "opencode"} - {backend}:
        assert store.config.to_payload()["agents"][other] == before["agents"][other]


def test_deduplicated_auth_reversal_restores_every_physical_config(monkeypatch, tmp_path):
    service, store, adapter, paths, auth = _shared_opencode(monkeypatch, tmp_path)
    before = {path: path.read_bytes() for path in [*paths, auth]}
    previous = store.config.to_payload()
    ids = [row["id"] for row in service.migration_scan()["items"]]
    assert len(ids) == 1
    apply = NativeFileEdit.apply
    cleaned = []

    def fail_second(edit, *, reverse=False):
        if not reverse:
            cleaned.append(edit.path)
            if len(cleaned) == 2:
                raise OSError("fixture interrupted native cleanup")
        return apply(edit, reverse=reverse)

    monkeypatch.setattr(NativeFileEdit, "apply", fail_second)
    with pytest.raises(ModelHubError):
        asyncio.run(service.migration_apply(ids))
    assert len(adapter.provisioned) == 1
    assert len(adapter.revoked) == 1
    assert store.config.to_payload() == previous
    assert {path: path.read_bytes() for path in before} == before
    assert service.migration_journal.load() is None


def test_deduplication_keeps_blocked_config_layers_visible(monkeypatch, tmp_path):
    service, _, adapter, paths, auth = _shared_opencode(monkeypatch, tmp_path)
    paths[1].write_text('{"provider":{"openai":{"options":{"apiKey":"{file:private}"}}}}')
    rows = service.migration_scan()["items"]
    assert len(rows) == 2
    assert {row["proposed_action"] for row in rows} == {"import", "reauth"}
    # The importable layer migrates; the {file:} layer stays native.
    blocked = paths[1].read_bytes()
    result = asyncio.run(service.migration_apply(
        [row["id"] for row in rows if row["proposed_action"] == "import"],
    ))
    assert result["applied"] == 1 and adapter.provisioned
    assert paths[1].read_bytes() == blocked
    assert [row["proposed_action"] for row in service.migration_scan()["items"]] == ["reauth"]


def _model_only_opencode(monkeypatch, tmp_path):
    service, store, adapter, paths, auth = _shared_opencode(monkeypatch, tmp_path)
    for path in paths:
        payload = json.loads(path.read_text())
        payload["provider"]["openai"].pop("options")
        path.write_text(json.dumps(payload))
    return service, store, adapter, paths, auth


@pytest.mark.parametrize("change", ["model", "credential", "new_layer"])
def test_cleanup_guards_model_only_and_absent_layers(monkeypatch, tmp_path, change):
    service, store, _, paths, auth = _model_only_opencode(monkeypatch, tmp_path)
    items = scan_native_configs(
        store.load(), home=service.migration_home,
        project_roots=service.migration_project_roots(), mask_credential=lambda _: "***",
    )
    assert len(items) == 1
    edits = plan_native_cleanup(
        items, home=service.migration_home, project_roots=service.migration_project_roots(),
    )
    changed_path = paths[1] if change != "new_layer" else paths[1].with_suffix(".jsonc")
    payload = json.loads(paths[1].read_text())
    if change == "model":
        payload["provider"]["openai"]["models"]["manual-1"]["name"] = "变化"
    else:
        payload["provider"]["openai"]["options"] = {"apiKey": "fixture-new-login"}
    changed_path.write_text(json.dumps(payload))
    with pytest.raises(TakeoverStateError):
        for edit in edits:
            edit.check()
    assert "fixture-shared" in auth.read_text()


def test_model_only_cleanup_preserves_bytes_and_guards_through_recovery(monkeypatch, tmp_path):
    service, store, _, paths, _ = _model_only_opencode(monkeypatch, tmp_path)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    items = scan_native_configs(
        store.load(), home=service.migration_home,
        project_roots=service.migration_project_roots(), mask_credential=lambda _: "***",
    )
    edits = [
        NativeFileEdit.from_payload(edit.to_payload())
        for edit in plan_native_cleanup(
            items, home=service.migration_home, project_roots=service.migration_project_roots(),
        )
    ]
    assert set(paths) <= {edit.path for edit in edits if edit.before == edit.after}
    for edit in edits:
        edit.check()
        edit.apply()
        edit.check(applied=True)
        edit.apply(reverse=True)
        edit.check()
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths} == before


@pytest.mark.parametrize("change", ["model", "credential", "new_layer"])
def test_async_provision_cannot_hide_changed_model_only_layer(monkeypatch, tmp_path, change):
    service, store, adapter, paths, auth = _model_only_opencode(monkeypatch, tmp_path)
    ids = [row["id"] for row in service.migration_scan()["items"]]
    before = store.config.to_payload()
    original_auth = auth.read_bytes()
    provision = adapter.provision_credential
    changed_path = paths[1] if change != "new_layer" else paths[1].with_suffix(".jsonc")

    async def change_after_plan(*args, **kwargs):
        ref = await provision(*args, **kwargs)
        payload = json.loads(paths[1].read_text())
        if change == "model":
            payload["provider"]["openai"]["models"]["manual-1"]["name"] = "变化"
        else:
            payload["provider"]["openai"]["options"] = {"apiKey": "fixture-new-login"}
        changed_path.write_text(json.dumps(payload))
        return ref

    adapter.provision_credential = change_after_plan
    with pytest.raises(ModelHubError) as error:
        asyncio.run(service.migration_apply(ids))
    assert error.value.code == "migration_configuration_blocked"
    assert store.config.to_payload() == before
    assert auth.read_bytes() == original_auth
    assert len(adapter.provisioned) == len(adapter.revoked) == 1
    assert service.migration_journal.load() is None
