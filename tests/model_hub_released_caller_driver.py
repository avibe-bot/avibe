"""Subprocess driver: import only pinned v3.2.5 product code via PYTHONPATH.

The package manifest and executable are test fixtures; RPC/service/adapter/
installer/supervisor selection is released code, not a copied decision model.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import re
import subprocess
import sys


async def main(spec):
    from config import paths
    from config.v2_config import ModelHubConfig, ModelHubSourceConfig, ModelHubSourceStateConfig
    from core.handlers.model_hub.events import BoundedEventLog
    from core.handlers.model_hub.rpc import dispatch_model_hub_rpc
    from core.handlers.model_hub.service import ModelHubService
    from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
    from vibe.model_hub_runtime.installer import EngineRuntimeManager
    from vibe.model_hub_runtime.state import EngineStateStore
    from vibe.model_hub_runtime.supervisor import EngineSupervisor, EngineUnavailableError

    # Assert the test did not silently import the current checkout's caller.
    import vibe.model_hub_runtime.adapter as released_adapter
    assert Path(released_adapter.__file__).is_relative_to(Path(spec["released_source"]))
    assert paths.get_state_dir().is_relative_to(Path(spec["service"]).parent)
    launches = []
    native_popen, native_run = subprocess.Popen, subprocess.run
    runtime = Path(spec["runtime"])

    def version_probe(command, *args, **kwargs):
        binary = Path(command[0])
        if (runtime / "versions") in binary.parents:
            assert command[1:] == ["--help"], command
            version = re.search(rb"CLIProxyAPI Version:\s*([\w.-]+)", binary.read_bytes()).group(1)
            stdout = b"CLIProxyAPI Version: " + version + b"\n"
            if kwargs.get("text"):
                stdout = stdout.decode()
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="" if isinstance(stdout, str) else b"")
        return native_run(command, *args, **kwargs)

    def spawn(command, *args, **kwargs):
        launches.append(command[0])
        return native_popen([sys.executable, spec["fixture_engine"], *command[1:]], *args, **kwargs)

    # The released installer executes --help. Emulate that fixture protocol on
    # all native platforms; digest and installed identity checks stay real.
    subprocess.run = version_probe
    manager = EngineRuntimeManager(runtime_dir=runtime, offline=True)
    manager.spec = replace(manager.spec, manifest_resource=spec["old_package_manifest"])
    assert manager.manifest_path is None and manager.manifest_url is None
    supervisor = EngineSupervisor(
        installer=manager, state_store=EngineStateStore(Path(spec["state"])), process_factory=spawn, startup_timeout=15,
    )
    adapter = CLIProxyEngineAdapter(supervisor=supervisor)

    class Store:
        def __init__(self):
            self.config = ModelHubConfig(enabled=True, sources=[ModelHubSourceConfig(
                id="src_fixture123", kind="subscription", vendor="anthropic", display_name="Fixture",
                protocol="anthropic", supply_channel="hub", billing="monthly",
                state=ModelHubSourceStateConfig(status="standby"), models=[],
            )])

        def load(self):
            return self.config

        def save(self, config):
            self.config = config

    root = Path(spec["service"])
    service = ModelHubService(
        store=Store(), adapter=adapter, events=BoundedEventLog(root / "events.json"),
        migration_home=root / "native-home", models_dev_catalog=lambda: {},
    )
    resume = None
    result = {"launches": launches, "prepare_succeeded": False, "rpc_error": None}
    try:
        if spec["operation"] == "prepared_demand":
            await adapter.ensure_installed()
            result["prepare_succeeded"] = True
            await adapter.start()
        else:
            prepare_entered = asyncio.Event()

            async def never_successfully_prepare(*_args, **_kwargs):
                prepare_entered.set()
                if spec["startup"] == "pending":
                    await asyncio.Event().wait()
                raise EngineUnavailableError("models.engine.install_failed", reason="fixture_prepare_failed")

            adapter.ensure_installed = never_successfully_prepare
            # Run the actual released startup-resume coroutine, not a synthetic
            # boolean. OAuth/reauth is dispatched while prepare is pending or
            # after that coroutine has caught the failed prepare.
            resume = asyncio.create_task(service._resume_runtime_intent())
            service._runtime_resume_task = resume
            await asyncio.wait_for(prepare_entered.wait(), 10)
            if spec["startup"] == "failed":
                await resume
            payload = ({"oauth": {"vendor": "anthropic", "channel": "hub"}}
                       if spec["operation"] == "oauth_start" else {
                           "source_id": "src_fixture123", "reauth": {"acknowledge_irreversible": True},
                       })
            try:
                await dispatch_model_hub_rpc(service, spec["operation"], payload)
            except Exception as exc:
                # The fixture supplies local health/auth inventory, not a real
                # provider OAuth URL. Selection proof is separate from OAuth
                # success and must not claim a provider authorization occurred.
                result["rpc_error"] = getattr(exc, "code", type(exc).__name__)
        result["selected_path"] = manager.status().get("path")
        return result
    finally:
        if resume is not None:
            resume.cancel()
            await asyncio.gather(resume, return_exceptions=True)
            service._runtime_resume_task = None
        await adapter.stop()


if __name__ == "__main__":
    from model_hub_engine_test_environment import install_child_audit

    spec = json.loads(Path(sys.argv[1]).read_text())
    install_child_audit(Path(spec["service"]).parent)
    result = asyncio.run(main(spec))
    Path(spec["result"]).write_text(json.dumps(result))
