"""Child-only bridge to the normal installed Avibe service, CLI, and Model Hub IPC.

No Controller/Agent replacement, no storage seeding, no provider implementation.
The supervisor supplies a fresh AVIBE_HOME before this module imports Avibe.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import runpy
import sys


def initialize(workdir: Path) -> dict:
    from config import paths
    from config.v2_config import V2Config

    config_path = paths.get_config_path()
    if config_path.exists():
        raise RuntimeError("refusing to overwrite an existing Avibe configuration")
    config = V2Config.default()
    config.runtime.default_cwd = str(workdir.resolve())
    config.platforms.enabled = []
    config.platforms.primary = config.platform = "avibe"
    for backend in (config.agents.claude, config.agents.codex, config.agents.opencode):
        backend.enabled = backend.auto_update = False
    config.update.auto_update = config.update.notify_admins = False
    config.update.check_interval_minutes = 0
    config.ui.open_browser = False
    config.ui.show_agent_activity = True
    config.remote_access.vibe_cloud.enabled = False
    config.setup_completed = True
    # Empty-home startup must not fetch or start an engine. The normal Hub
    # runtime_start operation below owns that transition, not a hand-written key.
    config.model_hub.enabled = False
    config.save(config_path)
    from vibe.runtime import get_service_main_path
    try:
        version = importlib.metadata.version("avibe-os")
    except importlib.metadata.PackageNotFoundError:
        version = None
    import vibe
    return {"service_main": str(get_service_main_path()), "avibe_version": version,
            "python": sys.version,
            "backend_catalog": str(Path(vibe.__file__).parent / "data" / "backend_models.json")}


async def configure(profile: dict, key: str) -> dict:
    from vibe.model_hub_client import ModelHubRemoteService

    hub = ModelHubRemoteService()
    await hub.runtime_start()
    created = await hub.create_source({
        "kind": "api_key", "vendor": "custom", "display_name": "Benchmark provider",
        "base_url": profile["base_url"], "protocol": profile["protocol"], "key": key,
        "save_unverified": True,
        "models": [{"id": profile["model"], "origin": "manual",
                    "reasoning_efforts": [profile["reasoning_effort"]]}],
    })
    source_id = created["source"]["id"]
    supply = await asyncio.to_thread(hub.get_agent_sources, "vibey")
    baseline = supply.get("catalog_models", [])
    chosen = {
        "id": profile["model"], "origin": "manual",
        "context_window": profile["context_window"],
        "max_output_tokens": profile["max_output_tokens"],
        "input_modalities": ["text"], "output_modalities": ["text"],
        "supports_tools": True, "supports_reasoning": True,
        "reasoning_efforts": [profile["reasoning_effort"]],
    }
    await hub.set_agent_sources("vibey", {"order": [source_id]})
    await hub.set_agent_models("vibey", baseline, [chosen])
    await hub.set_agent_chain("vibey", profile["model"], {
        "hops": [{"source_id": source_id, "model_id": profile["model"]}],
    })
    return {"source_id": source_id, "model": profile["model"], "protocol": profile["protocol"],
            "runtime": await hub.runtime_status()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["initialize", "serve", "health", "configure", "usage", "versions"])
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args(argv)
    if not os.environ.get("AVIBE_HOME") or not os.environ.get("AVIBE_BENCHMARK_OWNER"):
        parser.error("a supervisor-owned isolated AVIBE_HOME is required")
    try:
        if args.command == "initialize":
            if args.workdir is None:
                parser.error("initialize requires --workdir")
            result = initialize(args.workdir)
        elif args.command == "serve":
            from vibe.runtime import get_service_main_path
            runpy.run_path(str(get_service_main_path()), run_name="__main__")
            return 0
        elif args.command == "health":
            from vibe.internal_client import health_identity_sync
            identity = health_identity_sync()
            result = {"ready": identity is not None, "identity": identity}
        elif args.command == "configure":
            if args.profile is None:
                parser.error("configure requires --profile")
            profile = json.loads(args.profile.read_text(encoding="utf-8"))
            # Only this short-lived helper receives the key, over a private pipe.
            key = sys.stdin.read(65537)
            if not key.strip() or len(key) > 65536:
                raise ValueError("missing or oversized credential")
            result = asyncio.run(configure(profile, key))
        elif args.command == "usage":
            from vibe.model_hub_client import ModelHubRemoteService
            result = asyncio.run(ModelHubRemoteService().usage_summary(window="24h"))
        else:
            result = {"python": sys.version, "packages": {
                dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions()
                if dist.metadata.get("Name")
            }}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Upstream exception text may echo the key. Never print it on the bridge.
        print(json.dumps({"ok": False, "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
