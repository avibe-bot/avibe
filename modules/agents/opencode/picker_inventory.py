"""Account-scoped Direct picker proof. No native defaults, sessions, Hub or CLI probes.

Only transparent credentials are supported: native API keys and static provider
config. OpenAI OAuth supplies a workspace ID but not a person; any such profile
is unsupported. Unknown auth mechanisms,
plugins, substitutions and ambiguous config layers are not identities. A key
is portable between the UI and controller *only* for the same native profile.
The binary build belongs to the live probe, not the last-good inventory key.

Discovery is deliberately narrower than an OpenCode turn: no adopted process
or wrapper, no previously unused cwd, no Hub overlay. It reads the native
/config and connected /provider IDs, then takes their full /config/providers
rows, never the registry's default/recommended models. An unknown connected
provider rejects the whole observation; it is not a partial or empty menu.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from core.handlers.direct_model_menu import PickerModel
from vibe.native_inventory_files import read_native_file as _read
from vibe.opencode_config import get_opencode_auth_path, parse_jsonc_object

from .caller_context import PLUGIN_FILENAME, PLUGIN_SOURCE
from .client_manager import _binary_identity, _digest

# These native providers have a simple environment API-key credential. Do not
# infer a provider from an arbitrary *_TOKEN, a helper or a cloud SDK profile.
_GRANTS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "opencode": "OPENCODE_API_KEY",
}
_FILTERS = ("enabled_providers", "disabled_providers")
_MODEL_CONFIG = ("provider", *_FILTERS)
_MAX_FILE = 2 * 1024 * 1024


def _literal(s) -> bool:
    return isinstance(s, str) and bool(s.strip()) and not any(ref in s for ref in ("{env:", "{file:"))


def _object(path: Path, *, jsonc: bool = False) -> dict:
    raw = _read(path, _MAX_FILE)
    if raw is None:
        return {}
    value = parse_jsonc_object(raw.decode()) if jsonc else json.loads(raw)
    if not isinstance(value, dict) or not _literal(json.dumps(value)):
        raise ValueError("unproven native config")
    return value


def _roots(env):
    home = Path(env.get("HOME") or env.get("USERPROFILE") or str(Path.home()))
    data = env.get("XDG_DATA_HOME")
    auth = Path(data) / "opencode/auth.json" if data else get_opencode_auth_path(home)
    conf = Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "opencode"
    if not all(p.is_absolute() for p in (home, conf, auth)):
        raise ValueError("relative profile")
    return home, conf, auth


def _credentials(auth: Path) -> tuple[dict, str]:
    credentials = _object(auth)
    revision = _digest(credentials)
    for entry in credentials.values():
        if not isinstance(entry, dict) or entry.get("type") != "api" or not _literal(entry.get("key")):
            # Never attribute part of a mixed OAuth/API-key profile to a proven key.
            raise ValueError("no stable native credential")
    return credentials, revision


def _grant(grants: dict[str, str | None], pid: str, key: str | None) -> None:
    # Don't guess precedence between conflicting configured and inherited keys.
    value = _digest(key) if key is not None else None
    if pid in grants and grants[pid] != value:
        raise ValueError("ambiguous provider credentials")
    grants[pid] = value


def _profile(env: Mapping[str, str]):
    if any(k.startswith("OPENCODE_") and k != "OPENCODE_API_KEY" and v for k, v in env.items()):
        raise ValueError("alternate OpenCode profile")
    home, conf, auth = _roots(env)
    credentials, revision = _credentials(auth)
    # Include endpoint, organization, project, etc. from the same provider
    # namespaces. Unrelated application credentials are not OpenCode accounts.
    provider_env = {k: v for k, v in env.items() if k.startswith(("OPENAI_", "ANTHROPIC_", "OPENROUTER_"))}
    if "OPENCODE_API_KEY" in env:
        provider_env["OPENCODE_API_KEY"] = env["OPENCODE_API_KEY"]
    grants = {}
    for pid, entry in credentials.items():
        _grant(grants, pid, entry.get("key") if entry["type"] == "api" else None)
    for pid, name in _GRANTS.items():
        if _literal(key := env.get(name)):
            _grant(grants, pid, key)
    return _digest([str(home), str(conf), str(auth), credentials, provider_env]), grants, revision


def _reject_dotenv(directory: Path, env: Mapping[str, str]) -> None:
    # A launcher can load keys not present in its inherited environment. Do
    # not interpret a dotenv file or assume its contents match our process.
    names = {".env", ".env.local"}
    mode = env.get("NODE_ENV")
    if mode in ("test", "development", "production"):
        names.update((f".env.{mode}", f".env.{mode}.local"))
    if any((directory / name).exists() for name in names):
        raise ValueError("unproven dotenv profile")


def launch_proof(binary: str, inherited: Mapping[str, str]) -> str | None:
    """One in-memory observation at spawn; never reconstructed from an adopted record."""
    try:
        _reject_dotenv(Path.cwd(), inherited)
        profile, _, _ = _profile(inherited)
        _, build = _binary_identity(binary)
        return _digest([profile, build]) if "stat" in build else None
    except (OSError, ValueError, RecursionError):
        # An optional picker must never prevent a native turn from starting.
        return None


def _layer(doc: dict) -> dict:
    if any(doc.get(k) for k in ("plugin", "plugins", "enterprise", "extends")):
        raise ValueError("dynamic provider config")
    result = {k: doc[k] for k in _MODEL_CONFIG if k in doc}
    for key in _FILTERS:
        if key in result and not (isinstance(result[key], list) and all(_literal(x) for x in result[key])):
            raise ValueError("invalid provider filter")
    if not isinstance(result.get("provider", {}), dict):
        raise ValueError("invalid providers")
    return result


def _config(cwd: Path, env):
    home, conf, _ = _roots(env)
    # Don't guess merge precedence or attribute a parent's credentials to a
    # child outside its worktree. Only the effective XDG root and this exact
    # cwd contribute evidence. A competing ancestor layer is unsupported.
    sources = {conf / name for name in ("config.json", "opencode.json", "opencode.jsonc")}
    sources.update(cwd / name for name in ("opencode.json", "opencode.jsonc"))
    parents = []
    for parent in (cwd, *cwd.parents):
        parents.append(parent)
        if (parent / ".git").exists():
            break
    roots = {conf, home / ".opencode", *(p / ".opencode" for p in parents)}
    candidates = {p / name for p in (*roots, *parents) for name in ("opencode.json", "opencode.jsonc")}
    # Auto-loaded native plugins can supply credentials without a config key.
    for root in roots:
        for sub in ("plugin", "plugins"):
            plugins = root / sub
            if plugins.is_dir() and any(
                file != conf / "plugins" / PLUGIN_FILENAME
                or _read(file, len(PLUGIN_SOURCE.encode())) != PLUGIN_SOURCE.encode()
                for file in plugins.iterdir()
            ):
                raise ValueError("unproven native plugin")
    config, layers = {}, []
    for path in sorted(candidates | sources):
        layer = _layer(_object(path, jsonc=True))
        if not layer:
            continue
        if path not in sources or layer.keys() & config.keys():
            raise ValueError("ambiguous native provider layers")
        config.update(layer)
        layers.append((str(path), layer))
    return config, layers


@dataclass(frozen=True)
class _Scope:
    key: str
    profile: str
    revision: str
    config_digest: str
    providers: dict[str, str | None]
    cwd: str


def _scope(cwd: str) -> _Scope:
    env = os.environ.copy()
    directory = Path(cwd).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError("no project directory")
    _reject_dotenv(directory, env)
    profile, grants, revision = _profile(env)
    config, layers = _config(directory, env)
    providers = config.get("provider", {})
    for pid, settings in providers.items():
        if not isinstance(settings, dict):
            raise ValueError("invalid provider config")
        options = settings.get("options", {})
        if not isinstance(options, dict) or options.keys() - {"apiKey", "baseURL", "timeout"}:
            raise ValueError("unsupported credential or transport options")
        if "env" in settings and (pid not in _GRANTS or settings["env"] != [_GRANTS[pid]]):
            raise ValueError("unproven provider environment")
        if "npm" in settings and settings["npm"] not in (
            "@ai-sdk/openai-compatible",
            "@ai-sdk/openai",
            "@ai-sdk/anthropic",
            "@openrouter/ai-sdk-provider",
        ):
            raise ValueError("unsupported provider adapter")
        if _literal(options.get("apiKey")):
            _grant(grants, pid, options["apiKey"])
    for pid in grants.keys() - _GRANTS.keys():
        custom = providers.get(pid, {})
        if custom.get("npm") != "@ai-sdk/openai-compatible" or not _literal(custom.get("options", {}).get("baseURL")):
            raise ValueError("unsupported provider credential source")
    grants = {
        pid: cred
        for pid, cred in grants.items()
        if pid in config.get("enabled_providers", grants) and pid not in config.get("disabled_providers", [])
    }
    if not grants:
        raise ValueError("no scoped provider credential")
    return _Scope(
        _digest(["direct-picker-v2", str(directory), profile, layers]),
        profile,
        revision,
        _digest(config),
        grants,
        str(directory),
    )


def binding(config, cwd: str | None) -> tuple[str, str] | None:
    """Stable inventory key, plus an exact revision for in-flight validation only."""
    if not config.agents.opencode.enabled:
        return None
    try:
        scope = _scope(cwd or config.runtime.default_cwd)
        return scope.key, scope.revision
    except (ValueError, OSError, RecursionError):
        return None


async def discover(controller, binary: str, cwd: str) -> list[PickerModel]:
    """Pin only an already-serving Direct child; never acquire/adopt/prepare a launch."""
    from core.handlers.direct_model_menu import PickerModel

    agent = controller.agent_service.agents["opencode"]
    async with agent.borrow_picker_server() as server:
        scope = await asyncio.to_thread(_scope, cwd)
        _, build = await asyncio.to_thread(_binary_identity, binary)
        if not server.has_served_directory(scope.cwd):
            from modules.agents.direct_inventory import InventoryNotReady

            raise InventoryNotReady("cold directory")
        if "stat" not in build or server.picker_launch_proof != _digest([scope.profile, build]):
            raise ValueError("different OpenCode launch profile")
        # OpenCode can cache a directory's config from an earlier turn. Disk
        # equality alone cannot prove what this generation uses for this cwd.
        if (await server.direct_picker_get("/path", scope.cwd)).get("directory") != scope.cwd:
            raise ValueError("different effective OpenCode directory")
        actual = await server.direct_picker_get("/config", scope.cwd)
        # Refuse an error document or an effective upstream plugin we didn't
        # account for. A managed serve has this runtime policy even with no
        # user file; none of these GETs writes or changes that policy.
        tools = actual.get("tools")
        plugin_path = _roots(os.environ)[1] / "plugins" / PLUGIN_FILENAME
        plugins = actual.get("plugin", [])
        permission = actual.get("permission")
        managed = (isinstance(tools, dict) and tools.get("skill") is False) or (
            isinstance(permission, dict) and permission.get("skill") == "deny"
        )
        if (
            not managed
            or not isinstance(plugins, list)
            or any(p not in (str(plugin_path), plugin_path.as_uri()) for p in plugins)
            or actual.get("enterprise")
            or actual.get("plugins")
        ):
            raise ValueError("unproven effective OpenCode config")
        if _digest({k: actual[k] for k in _MODEL_CONFIG if k in actual}) != scope.config_digest:
            raise ValueError("different effective provider config")
        providers = await server.direct_picker_providers(scope.cwd)
        result = []
        for provider in providers:
            pid = provider["id"]
            if pid not in scope.providers:
                raise ValueError("unproven connected provider")
            options = provider.get("options", {})
            if not isinstance(options, dict):
                raise ValueError("invalid provider options")
            for live in (provider.get("key"), options.get("apiKey")):
                if live is not None and (not _literal(live) or _digest(live) != scope.providers[pid]):
                    raise ValueError("different effective provider credential")
            env_names = provider.get("env", [])
            if not isinstance(env_names, list) or any(name != _GRANTS.get(pid) or not name for name in env_names):
                raise ValueError("unproven provider environment")
            for model_id, info in provider["models"].items():
                if not _literal(model_id) or model_id != model_id.strip() or not isinstance(info, dict):
                    raise ValueError("invalid native model")
                if info.get("id", model_id) != model_id or info.get("providerID", pid) != pid:
                    raise ValueError("mismatched native model")
                variants = info.get("variants")
                if variants is not None and not (
                    isinstance(variants, dict) and all(_literal(k) and isinstance(v, dict) for k, v in variants.items())
                ):
                    raise ValueError("invalid native variants")
                result.append(
                    PickerModel(
                        f"{pid}/{model_id}",
                        info.get("name"),
                        reasoning_efforts=None if variants is None else tuple(variants),
                    )
                )
        if (
            await asyncio.to_thread(_scope, scope.cwd) != scope
            or (await asyncio.to_thread(_binary_identity, binary))[1] != build
        ):
            raise ValueError("OpenCode scope changed during discovery")
        return result
