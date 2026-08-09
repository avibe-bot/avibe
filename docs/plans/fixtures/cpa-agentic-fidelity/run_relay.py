#!/usr/bin/env python3
"""Run the fidelity probe through an isolated pinned CPA and a compatible relay."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from core.handlers.model_hub.adapter import SourceBinding  # noqa: E402
from vibe.model_hub_runtime.installer import EngineRuntimeManager  # noqa: E402
from vibe.model_hub_runtime.state import EngineStateStore  # noqa: E402
from vibe.model_hub_runtime.supervisor import EngineSupervisor  # noqa: E402


OPENAI_MODEL = "gpt-5.4-mini"
ANTHROPIC_MODEL = "claude-haiku-4-5"
ANTHROPIC_FALLBACK_MODEL = "claude-3-5-haiku-latest"
CHAT_SOURCE_PROTOCOL = "openai_chat"
PINNED_ENGINE_VERSION = "v7.2.95"
PINNED_ENGINE_MANIFEST = REPO_ROOT / "vibe/model_hub_runtime/cliproxyapi_manifest.json"


def _safe_relay_root(value: str) -> str | None:
    parsed = urllib.parse.urlsplit(value)
    if not (
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path in {"", "/"}
    ):
        return None
    return value.rstrip("/")


def _binding(
    *,
    source_id: str,
    vendor: str,
    protocol: str,
    base_url: str,
    credential_ref: str,
    models: tuple[str, ...],
) -> SourceBinding:
    return SourceBinding(
        source_id=source_id,
        vendor=vendor,
        protocol=protocol,
        base_url=base_url,
        credential_ref=credential_ref,
        allowed_origins=(),
        model_ids=models,
    )


def main() -> int:
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    relay_root = _safe_relay_root(os.environ.get("ANTHROPIC_BASE_URL", ""))
    if not anthropic_key or not openai_key or relay_root is None:
        print(json.dumps({"ok": False, "blocked": True, "missing": "owner relay environment"}, sort_keys=True))
        return 2
    openai_base = f"{relay_root}/v1"

    with tempfile.TemporaryDirectory(prefix="cpa-agentic-fidelity-") as temporary_name:
        temporary = Path(temporary_name)
        store = EngineStateStore(temporary / "state")
        anthropic_ref = store.store_api_key(
            anthropic_key,
            vendor="anthropic",
            protocol="anthropic",
            base_url=relay_root,
        )
        responses_ref = store.store_api_key(
            openai_key,
            vendor="openai",
            protocol="openai_responses",
            base_url=openai_base,
        )
        chat_ref = store.store_api_key(
            openai_key,
            vendor="custom",
            protocol=CHAT_SOURCE_PROTOCOL,
            base_url=openai_base,
        )
        sources = store.sync_sources(
            [
                _binding(
                    source_id="src_fidelityanthropic",
                    vendor="anthropic",
                    protocol="anthropic",
                    base_url=relay_root,
                    credential_ref=anthropic_ref,
                    models=(ANTHROPIC_MODEL, ANTHROPIC_FALLBACK_MODEL),
                ),
                _binding(
                    source_id="src_fidelityresponses",
                    vendor="openai",
                    protocol="openai_responses",
                    base_url=openai_base,
                    credential_ref=responses_ref,
                    models=(OPENAI_MODEL,),
                ),
                _binding(
                    source_id="src_fidelitychat",
                    vendor="custom",
                    protocol=CHAT_SOURCE_PROTOCOL,
                    base_url=openai_base,
                    credential_ref=chat_ref,
                    models=(OPENAI_MODEL,),
                ),
            ]
        )
        by_id = {source.source_id: source for source in sources}
        installer = EngineRuntimeManager(
            runtime_dir=temporary / "runtime",
            manifest_path=PINNED_ENGINE_MANIFEST,
            manifest_url="",
            offline=False,
        )
        supervisor = EngineSupervisor(installer=installer, state_store=store)
        try:
            if installer.contract_manifest().get("version") != PINNED_ENGINE_VERSION:
                print(json.dumps({"ok": False, "blocked": True, "reason": "pinned CPA manifest mismatch"}, sort_keys=True))
                return 2
            connection = supervisor.ensure_running()
            runtime_status = installer.status()
            if not runtime_status.get("installed") or runtime_status.get("version") != PINNED_ENGINE_VERSION:
                print(json.dumps({"ok": False, "blocked": True, "reason": "pinned CPA runtime mismatch"}, sort_keys=True))
                return 2
            child_env = os.environ.copy()
            child_env.update(
                {
                    "CPA_BASE_URL": connection.base_url,
                    "CPA_GATEWAY_TOKEN": connection.gateway_token,
                    "CPA_ANTHROPIC_QUALIFIED_MODEL": (
                        f"{by_id['src_fidelityanthropic'].prefix}/{ANTHROPIC_MODEL}"
                    ),
                    "CPA_ANTHROPIC_FALLBACK_QUALIFIED_MODEL": (
                        f"{by_id['src_fidelityanthropic'].prefix}/{ANTHROPIC_FALLBACK_MODEL}"
                    ),
                    "CPA_OPENAI_RESPONSES_QUALIFIED_MODEL": (
                        f"{by_id['src_fidelityresponses'].prefix}/{OPENAI_MODEL}"
                    ),
                    "CPA_OPENAI_CHAT_QUALIFIED_MODEL": (
                        f"{by_id['src_fidelitychat'].prefix}/{OPENAI_MODEL}"
                    ),
                }
            )
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("probe.py"))],
                cwd=REPO_ROOT,
                env=child_env,
                check=False,
            )
            return completed.returncode
        except Exception:  # noqa: BLE001
            print(json.dumps({"ok": False, "blocked": True, "reason": "isolated CPA startup failed"}, sort_keys=True))
            return 2
        finally:
            supervisor.stop()


if __name__ == "__main__":
    sys.exit(main())
