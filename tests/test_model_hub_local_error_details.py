"""Local OS failures survive the real gateway-to-notification boundaries."""

from __future__ import annotations

import errno
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from core.backend_failure import emit_backend_failure
from core.handlers.model_hub.provenance import BoundedProvenanceStore
from core.handlers.model_hub.service import ModelHubError
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from modules.im import MessageContext
from tests.test_model_hub_l3 import _assert_valid, _service, _source
from vibe.i18n import t as i18n_t
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.installer import EngineRuntimeManager
from vibe.model_hub_runtime.state import EngineStateStore
from vibe.model_hub_runtime.supervisor import EngineSupervisor


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EACCES, errno.EROFS, errno.EMFILE])
async def test_runtime_lock_error_reaches_notice_and_persisted_details(tmp_path, monkeypatch, code):
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
            assert response.status == 503
            payload = await response.json()
            summary = i18n_t("modelHub.errors.engine_down", "zh")
            assert payload["error"]["message"] == summary
        await emit_backend_failure(controller, context, "codex", f"API Error: 503 {summary}")
        notify, terminal = controller.emit_agent_message.call_args_list
        assert notify.args[1:3] == ("notify", summary)
        assert notify.kwargs["output"].metadata["local_error_detail"] == expected
        assert terminal.kwargs["output"].metadata["local_error_detail"] == expected
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


@pytest.mark.parametrize("error,expected", [
    (OSError(errno.ENOSPC, "private text"), f"[Errno {errno.ENOSPC}] {os.strerror(errno.ENOSPC)}"),
    (OSError("private text"), None),
    (RuntimeError("private text"), None),
    (OSError(999999, "private text"), None),
])
async def test_engine_call_only_exposes_recognized_os_reasons(tmp_path, error, expected):
    service = _service(tmp_path, sources=[_source("src_primary01", "Primary")])

    async def fail():
        raise error

    with pytest.raises(ModelHubError) as raised:
        await service._engine_call(fail())
    assert raised.value.code == "engine_down"
    assert raised.value.local_error_detail == expected
