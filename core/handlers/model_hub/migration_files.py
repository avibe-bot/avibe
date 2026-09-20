"""Plan native configuration cleanup without changing any native file."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

try:
    import tomllib
except ImportError:  # Python 3.10 uses the existing conditional dependency.
    import tomli as tomllib

from vibe.claude_config import get_claude_oauth_settings_backup_path, get_claude_settings_path
from vibe.codex_config import (
    CREDENTIALS_STORE_KEY,
    LEGACY_MANAGED_PROVIDER_IDS,
    MANAGED_PROVIDER_ID,
    _dump_toml,
    get_codex_config_paths,
)
from vibe.opencode_config import (
    get_opencode_auth_path,
    get_opencode_config_paths,
    parse_jsonc_object,
)

from .migration_journal import NativeFileEdit, TakeoverStateError, _read_regular

if TYPE_CHECKING:
    from .migration import NativeMigrationItem


class NativeReferenceError(TakeoverStateError):
    """A planned native configuration still consumes selected authentication."""

    def __init__(self, references: dict[str, tuple[str, ...]]) -> None:
        super().__init__("native configuration still references selected authentication")
        self.references = references


def env_reference(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\{env:([A-Za-z_][A-Za-z_0-9]*)\}", value)
    return match[1] if match else None


def env_references(value: object) -> tuple[str, ...]:
    """Inventory references even in unsupported header templates."""
    if isinstance(value, str):
        return tuple(re.findall(r"\{env:([A-Za-z_][A-Za-z_0-9]*)\}", value))
    if isinstance(value, dict):
        return tuple(dict.fromkeys(name for child in value.values() for name in env_references(child)))
    if isinstance(value, list):
        return tuple(dict.fromkeys(name for child in value for name in env_references(child)))
    return ()


def native_config_references(backend: str, payload: dict) -> frozenset[str]:
    """The native interpreters' references, independent of credential rows."""
    if backend == "opencode":
        return frozenset(env_references(payload))
    names: set[str] = set()
    if backend == "codex":
        providers = payload.get("model_providers", {})
        for provider in providers.values() if isinstance(providers, dict) else ():
            if not isinstance(provider, dict):
                continue
            name = provider.get("env_key")
            if isinstance(name, str) and name:
                names.add(name)
            headers = provider.get("env_http_headers", {})
            if isinstance(headers, dict):
                names.update(value for value in headers.values() if isinstance(value, str))
    return frozenset(names)


def _object(content: bytes, *, jsonc: bool = False) -> dict:
    try:
        result = parse_jsonc_object(content.decode()) if jsonc else json.loads(content)
    except (ValueError, UnicodeError):
        raise TakeoverStateError("native configuration cannot be parsed") from None
    if not isinstance(result, dict):
        raise TakeoverStateError("native configuration cannot be parsed")
    return result


def _json_bytes(payload: dict) -> bytes:
    # Preserve even unrelated escaped surrogate values without accepting them
    # as Hub model identities or failing halfway through native cleanup.
    return (json.dumps(payload, ensure_ascii=True, indent=2) + "\n").encode()


def claude_settings_paths(home: Path | None, projects: tuple[Path, ...]) -> tuple[Path, ...]:
    # An interrupted OAuth flow can restore its backup into settings.json.
    # It is therefore a credential writer's input, not an unrelated archive.
    return tuple(dict.fromkeys([
        get_claude_settings_path(home).absolute(),
        get_claude_oauth_settings_backup_path(home).absolute(),
        *(root / ".claude" / name for root in projects for name in ("settings.json", "settings.local.json")),
    ]))


def opencode_config_paths(home: Path | None, projects: tuple[Path, ...]) -> tuple[Path, ...]:
    candidates = get_opencode_config_paths(home)
    if home is None:
        config_home = os.environ.get("XDG_CONFIG_HOME")
        if config_home:
            candidates.append(Path(config_home).expanduser() / "opencode/opencode.json")
        directory = os.environ.get("OPENCODE_CONFIG_DIR")
        if directory:
            candidates.append(Path(directory).expanduser() / "opencode.json")
    paths = [path for candidate in candidates for path in (candidate, candidate.with_suffix(".jsonc"))]
    if home is None and os.environ.get("OPENCODE_CONFIG"):
        paths.append(Path(os.environ["OPENCODE_CONFIG"]).expanduser())
    paths.extend(
        root / name for root in projects
        for name in ("opencode.json", "opencode.jsonc", ".opencode/opencode.json", ".opencode/opencode.jsonc")
    )
    return tuple(dict.fromkeys(path.absolute() for path in paths))


def opencode_auth_path(home: Path | None) -> Path:
    if home is None and os.environ.get("XDG_DATA_HOME"):
        return Path(os.environ["XDG_DATA_HOME"]).expanduser() / "opencode/auth.json"
    return get_opencode_auth_path(home)


def opencode_catalog_path(home: Path | None) -> Path:
    if home is not None:
        return home / ".cache/opencode/models.json"
    cache_home = os.environ.get("XDG_CACHE_HOME")
    root = Path(cache_home).expanduser() if cache_home else Path.home() / ".cache"
    return root / "opencode/models.json"


def codex_config_paths(home: Path | None, projects: tuple[Path, ...]) -> tuple[Path, ...]:
    config_path, _ = get_codex_config_paths(home)
    return tuple(dict.fromkeys([config_path.absolute(), *(root / ".codex/config.toml" for root in projects)]))


def read_native_toml(path: Path) -> dict | None:
    content = _read_regular(path)
    if content is None:
        return None
    try:
        return tomllib.loads(content.decode())
    except (ValueError, UnicodeError):
        raise TakeoverStateError("native configuration cannot be parsed") from None


def read_native_config(path: Path, *, jsonc: bool = False) -> dict | None:
    content = _read_regular(path)
    return None if content is None else _object(content, jsonc=jsonc)


def planned_native_references(
    edits: dict[Path, NativeFileEdit], *, home: Path | None,
    project_roots: tuple[Path, ...] = (),
) -> dict[str, tuple[str, ...]]:
    """Read the exact after images, including layers with no migration row.

    Unselected layers become no-op guards so a new consumer introduced during
    proof cannot invalidate the decision to remove a shell assignment.
    """
    references: dict[str, list[str]] = {}
    for backend, paths in (
        ("claude", claude_settings_paths(home, project_roots)),
        ("codex", codex_config_paths(home, project_roots)),
        ("opencode", opencode_config_paths(home, project_roots)),
    ):
        for path in paths:
            if path not in edits:
                before = _read_regular(path)
                edits[path] = NativeFileEdit(path, before, before)
            content = edits[path].after
            if content is None:
                continue
            try:
                payload = (
                    tomllib.loads(content.decode()) if backend == "codex"
                    else _object(content, jsonc=backend == "opencode")
                )
            except (ValueError, UnicodeError):
                raise TakeoverStateError("native configuration cannot be parsed") from None
            for name in native_config_references(backend, payload):
                references.setdefault(name, []).append(str(path))
    return {name: tuple(paths) for name, paths in references.items()}


def plan_native_cleanup(
    items: list[NativeMigrationItem],
    *,
    home: Path | None,
    project_roots: tuple[Path, ...] = (),
    _include_shell: bool = True,
) -> list[NativeFileEdit]:
    """One before/after image per path, even when providers share a file.

    Only selected backend authentication is removed. Planning refuses malformed
    files instead of using the forgiving UI probes that can return an empty
    document after a parse failure.
    """
    edits: dict[Path, NativeFileEdit] = {}
    selected_backends = {item.backend for item in items}
    if any(set(item.required_backends) - selected_backends for item in items):
        raise TakeoverStateError("shared native credentials require joint consent")
    for item in items:
        for guard in item.file_snapshots:
            if guard.path in edits and edits[guard.path] != guard:
                raise TakeoverStateError("conflicting native credential snapshots")
            guard.check()
            edits[guard.path] = guard
    # Native-store resolution owns credential paths (including isolated secure
    # roots), while this planner owns one final file image for the whole batch.
    for item in items:
        for operation in (item.native_store_edit or {}).get("operations", []):
            if operation.get("kind") != "file":
                continue
            path = Path(operation["path"]).absolute()
            before = operation["before"]
            after = operation["after"]
            edit = NativeFileEdit(
                path,
                before["raw"].encode() if before["exists"] else None,
                after["raw"].encode() if after["exists"] else None,
            )
            if path in edits:
                previous = edits[path]
                if previous.before != edit.before or (previous.after != previous.before and previous != edit):
                    raise TakeoverStateError("conflicting native credential snapshots")
            edits[path] = edit

    def edit_json(path: Path, transform, *, jsonc: bool = False, guard_unchanged: bool = False) -> None:
        path = path.absolute()
        staged = edits.get(path)
        original = staged.before if staged else _read_regular(path)
        if guard_unchanged and staged is None:
            # Model-only and currently absent OpenCode layers also determine
            # the consented inventory. Preserve their exact snapshots through
            # asynchronous proof/provision and journaled cleanup/recovery.
            edits[path] = NativeFileEdit(path, original, original)
        if original is None:
            return
        working = staged.after if staged else original
        if working is None:
            return
        payload = _object(working, jsonc=jsonc)
        before = json.dumps(payload, sort_keys=True)
        transform(payload)
        if json.dumps(payload, sort_keys=True) != before:
            edits[path] = NativeFileEdit(path.absolute(), original, _json_bytes(payload))

    # Consent to inspect an opaque, verified-empty container does not transfer
    # any credential. It cannot authorize cleanup of another native store.
    backends = {item.backend for item in items if not item.native_store_placeholder}
    selected_secrets = {item.secret for item in items if item.secret}
    if "claude" in backends:
        def clear_settings(payload: dict) -> None:
            env = payload.get("env")
            if env is not None and not isinstance(env, dict):
                raise TakeoverStateError("native configuration cannot be parsed")
            if isinstance(env, dict):
                for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"):
                    if key != "ANTHROPIC_BASE_URL" and env.get(key) and env[key] not in selected_secrets:
                        raise TakeoverStateError("another native credential requires migration")
                    env.pop(key, None)
            if payload.get("apiKeyHelper"):
                raise TakeoverStateError("native credential helper requires configuration")
            payload.pop("apiKeyHelper", None)

        for path in claude_settings_paths(home, project_roots):
            edit_json(path, clear_settings, guard_unchanged=True)
        backup_path = get_claude_oauth_settings_backup_path(home).absolute()
        backup_edit = edits.get(backup_path)
        if backup_edit and backup_edit.before != backup_edit.after and backup_edit.after is not None:
            backup = _object(backup_edit.after)
            if set(backup) <= {"version", "env"} and not backup.get("env"):
                edits[backup_path] = NativeFileEdit(backup_path, backup_edit.before, None)

    if "codex" in backends:
        config_path, auth_path = get_codex_config_paths(home)

        def clear_auth(payload: dict) -> None:
            if payload.get("OPENAI_API_KEY") and payload["OPENAI_API_KEY"] not in selected_secrets:
                raise TakeoverStateError("another native credential requires migration")
            if payload.get("tokens") and not any(
                item.backend == "codex" and item.kind == "oauth_native"
                and not item.native_store_placeholder
                for item in items
            ):
                raise TakeoverStateError("another native credential requires migration")
            for key in ("OPENAI_API_KEY", "tokens", "auth_mode", "last_refresh"):
                payload.pop(key, None)

        edit_json(auth_path, clear_auth, guard_unchanged=True)
        for config_path in codex_config_paths(home, project_roots):
            content = _read_regular(config_path)
            if config_path in edits and content != edits[config_path].before:
                raise TakeoverStateError("native configuration changed")
            edits.setdefault(config_path, NativeFileEdit(config_path, content, content))
            if content is None:
                continue
            try:
                config = tomllib.loads(content.decode())
            except (ValueError, UnicodeError):
                raise TakeoverStateError("native configuration cannot be parsed") from None
            before = json.dumps(config, sort_keys=True, default=str)
            removable = {MANAGED_PROVIDER_ID, *LEGACY_MANAGED_PROVIDER_IDS}
            removable.update(item.native_provider_id for item in items if item.backend == "codex" and item.native_provider_id)
            providers = config.get("model_providers")
            if isinstance(providers, dict):
                for provider_id in removable:
                    provider = providers.get(provider_id)
                    if not isinstance(provider, dict):
                        continue
                    if provider.get("experimental_bearer_token") and provider["experimental_bearer_token"] not in selected_secrets:
                        raise TakeoverStateError("another native credential requires migration")
                    # Retain user labels, capabilities, and timeout preferences.
                    for key in ("base_url", "env_key", "experimental_bearer_token", "requires_openai_auth"):
                        provider.pop(key, None)
                    for header_field in ("http_headers", "env_http_headers"):
                        headers = provider.get(header_field)
                        if isinstance(headers, dict):
                            for name in list(headers):
                                if name.lower() in {"authorization", "x-api-key"}:
                                    headers.pop(name)
                if not providers:
                    config.pop("model_providers", None)
            if config.get("model_provider") in removable:
                config.pop("model_provider", None)
            profiles = config.get("profiles")
            if isinstance(profiles, dict):
                for profile in profiles.values():
                    if isinstance(profile, dict) and profile.get("model_provider") in removable:
                        profile.pop("model_provider")
            # A consented empty container is unrelated data, not replaced
            # authentication. Keep its selector so the recorded clean revision
            # remains observable (and a later native login is not hidden).
            if not any(item.backend == "codex" and item.native_store_placeholder for item in items):
                config.pop(CREDENTIALS_STORE_KEY, None)
            if json.dumps(config, sort_keys=True, default=str) != before:
                edits[config_path] = NativeFileEdit(config_path.absolute(), content, _dump_toml(config).encode())

    if "opencode" in backends:
        vendors = {item.vendor for item in items if item.backend == "opencode"}
        shell_values = dict(pair for item in items for pair in item.shell_values)

        def clear_providers(payload: dict) -> None:
            providers = payload.get("provider")
            if providers is None:
                return
            if not isinstance(providers, dict):
                raise TakeoverStateError("native configuration cannot be parsed")
            for vendor in vendors:
                provider = providers.get(vendor)
                if not isinstance(provider, dict):
                    continue
                options = provider.get("options")
                if isinstance(options, dict):
                    value = options.get("apiKey")
                    if value and value not in selected_secrets:
                        # The inventory bound the saved assignment, or proved
                        # its absence before selecting the auth.json fallback.
                        reference = (
                            isinstance(value, str)
                            and value.startswith("{env:") and value.endswith("}")
                            and (
                                not shell_values.get(value[5:-1])
                                or shell_values[value[5:-1]].strip() in selected_secrets
                            )
                        )
                        if not reference:
                            raise TakeoverStateError("another native credential requires migration")
                    options.pop("apiKey", None)
                    options.pop("baseURL", None)
                    if not options:
                        provider.pop("options", None)

        for path in opencode_config_paths(home, project_roots):
            edit_json(path, clear_providers, jsonc=True, guard_unchanged=True)

        def clear_provider_auth(payload: dict) -> None:
            for vendor in vendors:
                payload.pop(vendor, None)

        edit_json(opencode_auth_path(home), clear_provider_auth, guard_unchanged=True)
    if backends and _include_shell:
        from .migration_shell import cleanup_shell_profile, read_shell_profiles

        selected_values: dict[str, str] = {}
        for item in items:
            for name, value in item.shell_values:
                if name in selected_values and selected_values[name] != value:
                    raise TakeoverStateError("conflicting native credential snapshots")
                selected_values[name] = value
        if selected_values:
            references = planned_native_references(edits, home=home, project_roots=project_roots)
            auth_names = {name for item in items for name in item.shell_auth_variables}
            live_auth = {
                name: references[name]
                for name in selected_values.keys() & auth_names & references.keys()
            }
            if live_auth:
                raise NativeReferenceError(live_auth)
            # A Base URL may still serve a provider that had no credential to
            # import. Retain its assignment, even in a selected backend.
            selected_values = {
                name: value for name, value in selected_values.items()
                if name not in references
            }
        for profile in read_shell_profiles(home, frozenset(selected_values)):
            previous = edits.get(profile.path)
            if previous and (previous.before != profile.before or previous.mode != profile.mode):
                raise TakeoverStateError("native configuration changed")
            # Keep guards even when this profile contributes no selected line.
            edits[profile.path] = cleanup_shell_profile(profile, selected_values)
    for edit in edits.values():
        edit.check()
    return list(edits.values())
