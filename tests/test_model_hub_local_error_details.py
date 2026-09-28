"""Local OS failures survive the real gateway-to-notification boundaries."""

from __future__ import annotations

import errno
import json
import os
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from core.backend_failure import emit_backend_failure
from core.os_errors import local_error_detail
from core.handlers.model_hub.provenance import BoundedProvenanceStore
from core.handlers.model_hub.service import ModelHubError
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from modules.im import MessageContext
from tests.test_model_hub_l3 import _assert_valid, _service, _source
from tests.test_ui_session_stream import isolated_state  # noqa: F401
from vibe.i18n import t as i18n_t
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.installer import EngineRuntimeManager
from vibe.model_hub_runtime.state import EngineStateStore
from vibe.model_hub_runtime.supervisor import EngineSupervisor, EngineUnavailableError


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EACCES, errno.EROFS, errno.EMFILE])
@pytest.mark.parametrize("stage", ["install_lock", "invoke_spawn", "record_read", "record_write"])
async def test_runtime_error_reaches_notice_and_persisted_details(tmp_path, monkeypatch, code, stage):
    # Existing gateway tests replace sync_sources with a generic failure, which
    # misses the installer -> adapter boundary that discarded the real errno.
    installer = EngineRuntimeManager(runtime_dir=tmp_path / "engine", offline=True)

    def fail_lock():
        raise OSError(code, "private credential=fixture-secret", "/private/凭证.json")

    monkeypatch.setattr(installer, "_acquire_mutation_lock", fail_lock)
    state = EngineStateStore(tmp_path / "engine-state")
    runtime = CLIProxyEngineAdapter(
        supervisor=EngineSupervisor(installer=installer, state_store=state),
    )
    service = _service(tmp_path, sources=[_source("src_primary01", "Primary")])
    # Only the external model is fake. Installation, error conversion,
    # correlation, notification, and provenance storage use production code.
    service.adapter.ensure_installed = runtime.ensure_installed
    expected_status = 503
    if stage != "install_lock":
        from pathlib import Path
        from tests.test_model_hub_runtime import _binding, _fixture_supervisor

        def fail(*_args, **_kwargs):
            fail_lock()

        supervisor, state = _fixture_supervisor(tmp_path / "invocation", process_factory=fail)
        credential = state.store_api_key("fixture-key", base_url="https://api.example.test/v1")
        state.sync_sources([_binding(credential, source_id="src_primary01")])
        runtime = CLIProxyEngineAdapter(supervisor=supervisor)
        # Keep preparation successful so the real adapter's invocation boundary,
        # not sync_sources/ensure_installed, owns the failure.
        service.adapter.ensure_installed = AsyncMock()
        service.adapter.invoke = runtime.invoke
        if stage == "record_read":
            read_text = Path.read_text

            def read(path, *args, **kwargs):
                if path == supervisor._engine_record_path:
                    fail()
                return read_text(path, *args, **kwargs)

            monkeypatch.setattr(Path, "read_text", read)
        elif stage == "record_write":
            monkeypatch.setattr("vibe.model_hub_runtime.supervisor.write_atomic", fail)
        expected_status = 502
    gateway = ModelHubTurnGateway(service, language_provider=lambda: "zh")
    turn_id = "turn-local-error"
    base_url, token = await gateway.endpoint(
        "codex", process_scope="/repo", turn_id=turn_id,
        requested_model_id="shared-model", resolved_model_id="shared-model",
        source_id="src_primary01",
    )
    expected = f"[Errno {code}] {os.strerror(code)}"
    context = MessageContext(
        user_id="U1", channel_id="C1", platform="avibe",
        platform_specific={"turn_token": turn_id},
    )
    controller = SimpleNamespace(
        config=SimpleNamespace(language="zh"),
        model_hub_turn_gateway=gateway,
        emit_agent_message=AsyncMock(),
    )
    try:
        async with aiohttp.ClientSession(trust_env=False) as client:
            response = await client.post(
                f"{base_url}/v1/responses",
                json={"model": "shared-model", "input": "ping", "stream": False},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status == expected_status
            payload = await response.json()
            summary = i18n_t("modelHub.errors.engine_down", "zh")
            assert payload["error"]["message"] == summary
        await emit_backend_failure(controller, context, "codex", f"API Error: {expected_status} {summary}")
        notify, terminal = controller.emit_agent_message.call_args_list
        assert notify.args[1:3] == ("notify", summary)
        assert notify.kwargs["output"].metadata["local_error_detail"] == expected
        assert terminal.kwargs["output"].metadata["turn_failure_notification"]["local_error_detail"] == expected
        assert "private" not in json.dumps(notify.kwargs["output"].metadata)
    finally:
        await gateway.close()

    gateway.correlation.settle(turn_id, settled_by=SETTLED_BY_TERMINAL_RESULT)
    # A fresh store proves this is retained for later reads, not just live UI.
    record = BoundedProvenanceStore(service.provenance.path).get(turn_id)
    assert record["terminal_error"]["local_error_detail"] == expected
    assert record["terminal_error"]["reason"] == "engine_down"
    _assert_valid("turn-provenance.schema.json", record)
    assert "fixture-secret" not in service.provenance.path.read_text()
    assert "凭证" not in service.provenance.path.read_text()


@pytest.mark.parametrize("stage", ["pointer", "metadata", "binary", "probe"])
@pytest.mark.parametrize("code", [errno.EACCES, errno.EMFILE])
def test_installed_inspection_keeps_errno_without_stale_diagnostics(tmp_path, monkeypatch, stage, code):
    # Real installed inspection used to swallow errors before the supervisor
    # raised; fresh installation and synthetic supervisor errors miss that gap.
    from pathlib import Path
    from tests.test_managed_runtime import _write_subclass_runtime_fixture

    _archive, manifest = _write_subclass_runtime_fixture(tmp_path, "model-hub")
    installer = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)
    installed = installer.ensure()
    assert installed["ok"]
    supervisor = EngineSupervisor(installer=installer, state_store=EngineStateStore(tmp_path / "state"))
    binary = Path(installed["path"])
    target = {
        "pointer": installer.runtime_dir / "current.json",
        "metadata": binary.parent / installer.spec.metadata_filename,
        "binary": binary,
    }.get(stage)
    open_path = Path.open

    def fail(*_args, **_kwargs):
        raise OSError(code, "private reason", "/private/凭证")

    def open_file(path, *args, **kwargs):
        if path == target:
            fail()
        return open_path(path, *args, **kwargs)

    installer._verified_binary_cache = None
    with monkeypatch.context() as patch:
        if stage == "probe":
            patch.setattr("vibe.model_hub_runtime.installer.subprocess.run", fail)
        else:
            patch.setattr(Path, "open", open_file)
        with pytest.raises(EngineUnavailableError) as raised:
            supervisor._prepare_instance_locked()
        assert local_error_detail(raised.value) == f"[Errno {code}] {os.strerror(code)}"
    assert installer.status()["installed"]
    (installer.runtime_dir / "current.json").write_text("{}")
    with pytest.raises(EngineUnavailableError) as raised:
        supervisor._prepare_instance_locked()
    assert local_error_detail(raised.value) is None


def test_supervisor_health_error_keeps_errno_before_cleanup(tmp_path, monkeypatch):
    # Health projects request exceptions into a bool. Drive the real urllib
    # wrapper and supervisor, whose stop operation must not replace the cause.
    import urllib.error
    from tests.test_model_hub_runtime import _fixture_supervisor

    supervisor, _state = _fixture_supervisor(tmp_path, startup_timeout=0.05)

    def fail(*_args, **_kwargs):
        raise urllib.error.URLError(OSError(errno.EMFILE, "private", "/private/凭证"))

    monkeypatch.setattr(
        "vibe.model_hub_runtime.client.urllib.request.build_opener",
        lambda *_args: SimpleNamespace(open=fail),
    )
    with pytest.raises(EngineUnavailableError) as raised:
        supervisor.ensure_running()
    assert local_error_detail(raised.value) == f"[Errno {errno.EMFILE}] {os.strerror(errno.EMFILE)}"


@pytest.mark.parametrize("code", [errno.ENOSPC, None, 999999, True])
async def test_streamed_engine_outcome_retains_safe_detail_in_projection_and_record(tmp_path, code):
    # Stream settlement bypasses ModelHubError. Its live projection and durable
    # terminal record are separate consumers of the actual adapter outcome.
    from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind
    from core.handlers.model_hub.service import ResolvedInvocation
    from tests.test_model_hub_provenance import _live_registry

    service = _service(tmp_path, sources=[_source("src_primary01", "Primary")])
    registry = _live_registry(tmp_path)
    registry.begin_attempt(
        "turn-live", source_id="src_primary01", resolved_model_id="shared-model",
        channel="hub", via_mapping=False,
    )
    outcome = RawCallOutcome(
        kind=RawOutcomeKind.NETWORK_ERROR, http_status=200, error_code="engine_down",
        redacted_message=None, stream_started=True, model_id="shared-model",
        source_id="src_primary01", os_errno=code,
    )
    settlement = await service.settle_handle_outcome(
        ResolvedInvocation(
            backend="codex", requested_model_id="shared-model",
            source_id="src_primary01", source_label="Primary", model_id="shared-model",
            handle=None, outcome=None,
        ),
        outcome,
        termination_origin="upstream_terminal",
        record_attempt=lambda raw, decision: registry.finish_attempt("turn-live", outcome=raw, decision=decision),
    )
    expected = "[Errno 28] No space left on device" if code == errno.ENOSPC else None
    assert settlement.turn_outcome.local_error_detail == expected
    registry.settle("turn-live", settled_by=SETTLED_BY_TERMINAL_RESULT)
    record = BoundedProvenanceStore(tmp_path / "records.json").get("turn-live")
    assert record["terminal_error"].get("local_error_detail") == expected
    assert service.store.load().sources[0].state.status == "standby"
    assert service.events.list() == []


@pytest.mark.parametrize("error,expected", [
    (OSError(errno.ENOSPC, "private text"), f"[Errno {errno.ENOSPC}] {os.strerror(errno.ENOSPC)}"),
    (OSError("private text"), None),
    (RuntimeError("private text"), None),
    (OSError(999999, "private text"), None),
    (ssl.SSLError(ssl.SSL_ERROR_SSL, "private TLS detail"), None),
])
async def test_engine_call_only_exposes_recognized_os_reasons(tmp_path, error, expected):
    service = _service(tmp_path, sources=[_source("src_primary01", "Primary")])

    async def fail():
        raise error

    with pytest.raises(ModelHubError) as raised:
        await service._engine_call(fail())
    assert raised.value.code == "engine_down"
    assert raised.value.local_error_detail == expected


@pytest.mark.parametrize("code", [errno.EACCES, errno.EMFILE])
def test_spawn_failure_keeps_its_os_reason(tmp_path, code):
    # A real supervisor wraps Popen failures; a synthetic adapter exception
    # cannot catch the loss of its explicit cause.
    from tests.test_model_hub_runtime import _fixture_supervisor

    def spawn(*_args, **_kwargs):
        raise OSError(code, "private credential=fixture-secret", "/private/凭证")

    supervisor, _state = _fixture_supervisor(tmp_path, process_factory=spawn)
    with pytest.raises(EngineUnavailableError) as raised:
        supervisor.ensure_running()
    assert local_error_detail(raised.value) == f"[Errno {code}] {os.strerror(code)}"


@pytest.mark.parametrize("code", [errno.EACCES, errno.EMFILE])
def test_engine_binary_probe_failure_keeps_errno(tmp_path, monkeypatch, code):
    # The engine installer's version probe is another exception -> sentinel
    # boundary; a generic fixture installer does not execute that code.
    from tests.test_managed_runtime import _write_subclass_runtime_fixture

    _archive, manifest = _write_subclass_runtime_fixture(tmp_path, "model-hub")
    installer = EngineRuntimeManager(runtime_dir=tmp_path / "runtime", manifest_path=manifest)

    def fail(*_args, **_kwargs):
        raise OSError(code, "private reason", "/private/凭证")

    monkeypatch.setattr("vibe.model_hub_runtime.installer.subprocess.run", fail)
    result = installer.ensure()
    assert result["reason"] == "model_hub_engine_binary_not_runnable"
    assert result["os_errno"] == code


@pytest.mark.parametrize("stage", ["claim", "pointer", "existing_validation", "new_validation"])
@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EACCES])
def test_structured_installer_failures_keep_errno(tmp_path, monkeypatch, stage, code):
    # Every exception -> failure-dict boundary must preserve errno. The original
    # runtime-lock regression never enters the already-installed reuse branch.
    from tests.test_managed_runtime import _fixture_runtime_manager, _write_fixture_runtime_release

    manifest = tmp_path / "manifest.json"
    _write_fixture_runtime_release(tmp_path, manifest, label="errno", version="1.0.0")
    manager = _fixture_runtime_manager(tmp_path / "runtime", manifest_path=manifest)
    if stage in {"pointer", "existing_validation"}:
        assert manager.ensure()["ok"]

    def fail(*_args, **_kwargs):
        raise OSError(code, "private reason", "/private/凭证")

    kwargs = {}
    if stage == "claim":
        kwargs["on_resolved"] = fail
    elif stage == "pointer":
        (manager.runtime_dir / "current.json").unlink()
        monkeypatch.setattr(manager, "_write_current_pointer", fail)
    else:
        kwargs["validate_candidate"] = fail
    result = manager.ensure(**kwargs)
    assert not result["ok"]
    assert result["os_errno"] == code


@pytest.mark.parametrize("deferred", [False, True])
async def test_failure_snapshot_survives_durable_notice_replay(tmp_path, deferred):
    # Exercise the terminal-output -> durable Run -> owed notice -> replay path,
    # without any live gateway or retained provenance record to fall back to.
    from core.backend_failure import terminal_backend_failure_output
    from core.delivery_evidence import DeliveryEvidence
    from core.message_output import MessageOutput
    from core.scheduled_tasks import parse_scope_id
    from tests.test_harness_failure_visibility import _drain_service, _store

    store, requests = _store(tmp_path)
    run = requests.enqueue_agent_run(
        message="work", session_id="ses-local-error", agent_name="worker", agent_backend="codex",
    )
    assert requests.claim(run.id)
    context = MessageContext(
        user_id="U1", channel_id="C1", platform="avibe",
        platform_specific={"turn_token": "turn-local-error"},
    )
    expected = "[Errno 28] No space left on device"
    terminal = terminal_backend_failure_output(
        context, output=MessageOutput(metadata={"local_error_detail": expected}),
    )
    store.record_turn_run_outputs(
        [run.id], output_id="terminal", text="",
        provenance=terminal.provenance(context), terminal_status="failed", error="engine down",
        deferred_run_ids=[run.id] if deferred else [],
    )
    if deferred:
        assert store.owed_failure_notice(run.id) is None
        assert store.settle_deferred_run(run.id)
    notice = store.owed_failure_notice(run.id)
    assert notice["local_error_detail"] == expected
    controller = SimpleNamespace(emit_agent_message=AsyncMock())
    service = _drain_service(tmp_path, controller, store, requests)
    service._failure_notice_targets = lambda _run: [
        (parse_scope_id("avibe::project::local-error"), "ses-local-error"),
    ]
    service._build_context = AsyncMock(return_value=context)
    await service._emit_failure_notice(store.get_run(run.id), notice, DeliveryEvidence())
    output = controller.emit_agent_message.call_args.kwargs["output"]
    assert output.metadata["local_error_detail"] == expected
    assert output.metadata["replayed"] is True


@pytest.mark.parametrize("role,visible", [("owner", True), ("member", True), ("editor", False), ("viewer", False)])
@pytest.mark.parametrize("promoted", [False, True])
def test_local_details_authorized_in_history_and_live_events(
    isolated_state, tmp_path, monkeypatch, role, visible, promoted,
):
    # React gating is not authorization. The persisted transcript and the actual
    # mirror publication must each be projected before reaching a chat-only reader.
    from core import message_mirror
    from storage import messages_service
    from storage.db import create_sqlite_engine
    from tests.test_ui_session_stream import _make_session
    from vibe import ui_server
    from vibe.authorization import AuthorizationContext

    scope_id, session_id = _make_session(tmp_path)
    context = AuthorizationContext(instance_role=role, is_remote=True)
    metadata = {"event": "backend_failure", "local_error_detail": "[Errno 28] No space left on device"}
    if promoted:
        with create_sqlite_engine().begin() as conn:
            messages_service.append(
                conn, scope_id=scope_id, session_id=session_id, platform="avibe",
                author="agent", message_type="notify", text="hidden failure",
                native_message_id="local-failure", metadata={"delivery_suppressed": True},
            )
    published = []
    monkeypatch.setattr(message_mirror, "_publish_session_message", published.append)
    monkeypatch.setattr("core.web_push_notifications.maybe_notify_inbox_message", lambda *_args: None)
    message_mirror.persist_agent_message(
        MessageContext(
            user_id="U1", channel_id="C1", platform="avibe",
            platform_specific={"agent_session_id": session_id},
        ),
        "notify", "gateway unavailable", metadata=metadata, native_message_id="local-failure",
    )
    [row] = published
    with create_sqlite_engine().connect() as conn:
        for window in ({}, {"tail": True}, {"around_id": row["id"]}):
            history = messages_service.list_session_messages(
                conn, session_id=session_id, authorization_context=context, **window,
            )
            assert ("local_error_detail" in history["messages"][0]["metadata"]) is visible
        public = messages_service.get_message(conn, row["id"])
        assert "local_error_detail" not in public["metadata"]
    payload = ui_server._workbench_event_payload_for_context(
        context, "message.new", json.dumps({"type": "message.new", "data": row}),
    )
    assert ("local_error_detail" in json.loads(payload)["data"]["metadata"]) is visible


@pytest.mark.parametrize("stage", ["manifest_path", "manifest_cache", "manifest_package", "manifest_download", "archive"])
@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EMFILE])
def test_installer_helper_failures_keep_only_the_current_errno(tmp_path, monkeypatch, stage, code):
    # Helpers return sentinels before ensure builds its failure result. Exercise
    # actual dependency wrappers and reads, not a fixture failure dictionary.
    import urllib.error
    from core import managed_runtime
    from pathlib import Path
    from tests.test_managed_runtime import _fixture_runtime_manager, _write_fixture_runtime_release

    manifest = tmp_path / "unused.json"
    _write_fixture_runtime_release(tmp_path, manifest, label="helper-errno", version="1.0.0")
    manager = _fixture_runtime_manager(tmp_path / "runtime", manifest_path=manifest)
    failed_read = manifest
    if stage in {"manifest_cache", "manifest_download"}:
        manager.manifest_path = None
        manager.manifest_url = manifest.as_uri()
    if stage == "manifest_cache":
        manager.offline = True
        failed_read = manager._remote_manifest_cache_path()
        failed_read.parent.mkdir(parents=True)
        failed_read.write_bytes(manifest.read_bytes())
    if stage == "manifest_package":
        manager.manifest_path = None

    read_bytes, open_path, urlopen = Path.read_bytes, Path.open, managed_runtime.urllib.request.urlopen

    def fail():
        raise OSError(code, "private reason", "/private/凭证")

    def read(path):
        if path == failed_read:
            fail()
        return read_bytes(path)

    def open_file(path, *args, **kwargs):
        if path.parent == manager.runtime_dir / "downloads" and path.suffix == ".tmp":
            fail()
        return open_path(path, *args, **kwargs)

    def open_url(url, *args, **kwargs):
        if url == manager.manifest_url:
            raise urllib.error.URLError(OSError(code, "private reason", "/private/凭证"))
        return urlopen(url, *args, **kwargs)

    with monkeypatch.context() as patch:
        if stage == "manifest_package":
            patch.setattr(managed_runtime.package_resources, "files", lambda _package: tmp_path)
        if stage in {"manifest_path", "manifest_cache", "manifest_package"}:
            patch.setattr(Path, "read_bytes", read)
        elif stage == "manifest_download":
            patch.setattr(managed_runtime.urllib.request, "urlopen", open_url)
        else:
            patch.setattr(Path, "open", open_file)
        failed = manager.ensure()
    assert not failed["ok"]
    assert failed["os_errno"] == code
    manager.manifest_path, manager.manifest_url, manager.offline = manifest, None, False
    succeeded = manager.ensure()
    assert succeeded["ok"] and "os_errno" not in succeeded
    manifest.write_text("{}")
    invalid = manager.ensure()
    assert not invalid["ok"] and "os_errno" not in invalid
