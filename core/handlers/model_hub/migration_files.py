"""Plan native configuration cleanup without changing any native file."""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
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
from vibe.native_oauth_store import codex_edit_keeping_api_key
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


def _keep(payload: dict) -> None:
    """Guard a file whose static credentials stay native."""


def native_store_items(
    items: list[NativeMigrationItem], *, clean_api_keys: bool,
) -> list[NativeMigrationItem]:
    """Items with the native credential-store edit (file or Keychain) to apply.

    A subscription login is always withdrawn. A Codex store keeps its static
    key unless cleanup was requested and this batch carries that key, and a
    store left unchanged becomes a compare-only guard so a change during the
    migration is still detected.
    """
    withdrawn = frozenset(
        item.secret.strip() for item in items
        if clean_api_keys and item.backend == "codex" and item.kind != "oauth_native" and item.secret
    )
    return [
        replace(item, native_store_edit=codex_edit_keeping_api_key(
            item.native_store_edit, withdrawn_keys=withdrawn,
        ))
        if item.backend == "codex" and item.native_store_edit else item
        for item in items
    ]


def _codex_store_holds_api_key(
    items: list[NativeMigrationItem], auth_path: Path, edits: dict[Path, NativeFileEdit],
) -> bool:
    """Whether the Codex credential store (auth.json or Keychain) holds a static key."""
    staged = edits.get(auth_path.absolute())
    raw = staged.before if staged else _read_regular(auth_path.absolute())
    values: list[object] = [raw.decode(errors="replace")] if raw is not None else []
    for item in items:
        if item.backend != "codex":
            continue
        for operation in (item.native_store_edit or {}).get("operations", []):
            before = operation.get("before")
            if isinstance(before, dict) and before.get("exists"):
                values.append(before.get("raw", before.get("value")))
    for value in values:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, dict) and isinstance(value.get("OPENAI_API_KEY"), str) and value["OPENAI_API_KEY"].strip():
            return True
    return False


def plan_native_cleanup(
    items: list[NativeMigrationItem],
    *,
    home: Path | None,
    project_roots: tuple[Path, ...] = (),
    _include_shell: bool = True,
    clean_api_keys: bool = True,
) -> list[NativeFileEdit]:
    """One before/after image per path, even when providers share a file.

    Only selected backend authentication is removed. Planning refuses malformed
    files instead of using the forgiving UI probes that can return an empty
    document after a parse failure. Without ``clean_api_keys`` static keys are
    copied, not withdrawn: a Hub launch pins its own connection above them.
    Subscription grants rotate and keep a single owner, so they are always
    withdrawn. Unchanged files are still guarded.
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
    for item in native_store_items(items, clean_api_keys=clean_api_keys):
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
    # Consent is per backend: equal bytes selected for another backend never
    # authorize removing a credential this backend's scan kept native.
    selected_by_backend: dict[str, set[str]] = {}
    for item in items:
        if item.secret:
            selected_by_backend.setdefault(item.backend, set()).add(item.secret)
    oauth_backends = {
        item.backend for item in items
        if item.kind == "oauth_native" and not item.native_store_placeholder
    }

    def selected_api_key(value: object, backend: str) -> bool:
        # Producers normalize static keys for proof/custody. Exact raw bytes
        # remain in the checked snapshots and journal before-images; this
        # comparison does not replace their consent or concurrency checks.
        return isinstance(value, str) and value.strip() in selected_by_backend.get(backend, set())

    if "claude" in backends:
        claude_keys = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
        # Consent is per field too: equal bytes carried from one Claude field
        # never authorize removing another field the scan kept native.
        selected_by_field: dict[str, set[str]] = {}
        for item in items:
            if item.backend == "claude" and item.secret:
                for field in (item.native_field, *item.shell_auth_variables):
                    if field:
                        selected_by_field.setdefault(field, set()).add(item.secret)

        def claude_retained(key: str, value: object) -> bool:
            selected = isinstance(value, str) and value.strip() in selected_by_field.get(key, set())
            return bool(value) and not selected

        # Claude merges its settings layers, so a credential kept native in any
        # one of them still sends to the Base URL another layer names.
        claude_auth_retained = False
        for path in claude_settings_paths(home, project_roots):
            staged = edits.get(path.absolute())
            raw = staged.before if staged else _read_regular(path.absolute())
            if raw is None:
                continue
            layer = _object(raw)
            env = layer.get("env")
            if layer.get("apiKeyHelper") or (
                isinstance(env, dict) and any(claude_retained(key, env.get(key)) for key in claude_keys)
            ):
                claude_auth_retained = True

        def clear_settings(payload: dict) -> None:
            env = payload.get("env")
            if env is not None and not isinstance(env, dict):
                raise TakeoverStateError("native configuration cannot be parsed")
            # A credential the Hub cannot carry stays native, with the Base
            # URL it may depend on. Hub launches pin their own connection.
            if isinstance(env, dict):
                for key in claude_keys:
                    if not claude_retained(key, env.get(key)):
                        env.pop(key, None)
                if not claude_auth_retained:
                    env.pop("ANTHROPIC_BASE_URL", None)

        for path in claude_settings_paths(home, project_roots):
            edit_json(path, clear_settings if clean_api_keys else _keep, guard_unchanged=True)
        backup_path = get_claude_oauth_settings_backup_path(home).absolute()
        backup_edit = edits.get(backup_path)
        if backup_edit and backup_edit.before != backup_edit.after and backup_edit.after is not None:
            backup = _object(backup_edit.after)
            if set(backup) <= {"version", "env"} and not backup.get("env"):
                edits[backup_path] = NativeFileEdit(backup_path, backup_edit.before, None)

    if "codex" in backends:
        config_path, auth_path = get_codex_config_paths(home)

        def clear_auth(payload: dict) -> None:
            # Credentials outside the selection stay native (see Claude above);
            # without cleanup a selected key is copied and stays native too.
            if clean_api_keys and (
                not payload.get("OPENAI_API_KEY") or selected_api_key(payload["OPENAI_API_KEY"], "codex")
            ):
                payload.pop("OPENAI_API_KEY", None)
            if not payload.get("tokens") or any(
                item.backend == "codex" and item.kind == "oauth_native"
                and not item.native_store_placeholder
                for item in items
            ):
                for key in ("tokens", "last_refresh"):
                    payload.pop(key, None)
                # A kept key still authenticates native launches; only the login goes.
                if not payload.get("OPENAI_API_KEY") or payload.get("auth_mode") == "chatgpt":
                    payload.pop("auth_mode", None)

        edit_json(
            auth_path, clear_auth if clean_api_keys or "codex" in oauth_backends else _keep,
            guard_unchanged=True,
        )
        # A key kept beside the withdrawn login (selected or not) still routes
        # through the login's provider, so that routing stays too.
        codex_key_kept = not clean_api_keys and _codex_store_holds_api_key(items, auth_path, edits)
        managed_ids = {MANAGED_PROVIDER_ID, *LEGACY_MANAGED_PROVIDER_IDS}
        # An env_key is carried only when a selected row resolved it to a
        # selected key; an unresolved or unselected one stays native.
        carried_env_keys = {
            name for item in items if item.backend == "codex"
            for name, value in item.shell_values
            if value.strip() in selected_by_backend.get("codex", set())
        }

        def codex_auth_retained(provider_id: str, provider: dict) -> bool:
            env_key = provider.get("env_key")
            # A managed provider is Avibe-owned routing, but a key the batch did
            # not carry (e.g. an unimportable endpoint) still keeps it native.
            uncarried_key = bool(
                (provider.get("experimental_bearer_token")
                    and not selected_api_key(provider["experimental_bearer_token"], "codex"))
                or (env_key and env_key not in carried_env_keys)
            )
            if provider_id in managed_ids:
                return uncarried_key
            return uncarried_key or any(
                provider.get(field) for field in ("http_headers", "env_http_headers")
            )

        layers: list[tuple[Path, bytes | None, dict | None]] = []
        for config_path in codex_config_paths(home, project_roots):
            content = _read_regular(config_path)
            if config_path in edits and content != edits[config_path].before:
                raise TakeoverStateError("native configuration changed")
            edits.setdefault(config_path, NativeFileEdit(config_path, content, content))
            if content is None:
                layers.append((config_path, None, None))
                continue
            try:
                layers.append((config_path, content, tomllib.loads(content.decode())))
            except (ValueError, UnicodeError):
                raise TakeoverStateError("native configuration cannot be parsed") from None
        # Codex merges its layers, so authentication migration did not carry,
        # in any layer, keeps the whole provider native in every layer,
        # selectors included, or direct mode would stop using what was kept.
        retained_ids = {
            provider_id
            for _, _, config in layers if config is not None
            for provider_id, provider in (
                config["model_providers"].items() if isinstance(config.get("model_providers"), dict) else ()
            )
            if isinstance(provider, dict) and codex_auth_retained(provider_id, provider)
        }
        for config_path, content, config in layers:
            if config is None:
                continue
            before = json.dumps(config, sort_keys=True, default=str)
            # Without key cleanup a provider stays unless only the withdrawn
            # login routes through it.
            removable = set(managed_ids) if clean_api_keys else set()
            removable.update(
                item.native_provider_id for item in items
                if item.backend == "codex" and item.native_provider_id
                and (clean_api_keys or item.kind == "oauth_native")
            )
            if not clean_api_keys:
                removable -= {
                    item.native_provider_id for item in items
                    if item.backend == "codex" and (item.kind != "oauth_native" or codex_key_kept)
                }
            providers = config.get("model_providers")
            if isinstance(providers, dict):
                # A retained provider keeps its selectors and whatever
                # authentication was not carried, but a credential that WAS
                # carried still leaves the layer that supplied it: the Hub now
                # holds it, and a replay must not find it importable again.
                for provider_id in sorted(removable & retained_ids):
                    provider = providers.get(provider_id)
                    if not isinstance(provider, dict):
                        continue
                    token = provider.get("experimental_bearer_token")
                    if token and selected_api_key(token, "codex"):
                        provider.pop("experimental_bearer_token")
                    if provider.get("env_key") in carried_env_keys:
                        provider.pop("env_key")
            removable -= retained_ids
            if isinstance(providers, dict):
                for provider_id in sorted(removable):
                    provider = providers.get(provider_id)
                    if not isinstance(provider, dict):
                        continue
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
            # A kept key may live in the selected store, so only cleanup drops it.
            if clean_api_keys and not any(
                item.backend == "codex" and item.native_store_placeholder for item in items
            ):
                config.pop(CREDENTIALS_STORE_KEY, None)
            if json.dumps(config, sort_keys=True, default=str) != before:
                edits[config_path] = NativeFileEdit(config_path.absolute(), content, _dump_toml(config).encode())

    if "opencode" in backends:
        vendors = {item.vendor for item in items if item.backend == "opencode"}
        shell_values = dict(pair for item in items for pair in item.shell_values)

        absent = object()

        def auth_entry_retained(entry: object) -> bool:
            # An entry the Hub could not carry (OAuth, a key outside the
            # selection, or a value of no shape it reads) stays native, like
            # every other unselected credential. Only an absent entry and the
            # selected API key itself are free to go.
            return entry is not absent and not (
                isinstance(entry, dict)
                and "type" in entry and entry["type"] == "api"
                and selected_api_key(entry.get("key"), "opencode")
            )

        native_auth = read_native_config(opencode_auth_path(home)) or {}
        retained_auth = {vendor for vendor in vendors if auth_entry_retained(native_auth.get(vendor, absent))}

        # References a selected row read: bound to its saved assignment, or
        # proved empty before it selected the auth.json fallback. Any other
        # reference (unresolved, or never scanned into a selection) stays.
        carried_references = {
            name for item in items if item.backend == "opencode" for name in item.shell_variables
        }

        def api_key_retained(value: object) -> bool:
            if not value or selected_api_key(value, "opencode"):
                return False
            return not (
                isinstance(value, str)
                and value.startswith("{env:") and value.endswith("}")
                and value[5:-1] in carried_references
                and (
                    not shell_values.get(value[5:-1])
                    or shell_values[value[5:-1]].strip() in selected_by_backend.get("opencode", set())
                )
            )

        # OpenCode merges its layers, so a credential kept native in any one of
        # them, header auth or an unselected key, still sends to the endpoint
        # another layer names.
        for path in opencode_config_paths(home, project_roots):
            staged = edits.get(path.absolute())
            raw = staged.before if staged else _read_regular(path.absolute())
            if raw is None:
                continue
            providers = _object(raw, jsonc=True).get("provider")
            if not isinstance(providers, dict):
                continue
            for vendor in vendors:
                provider = providers.get(vendor)
                options = provider.get("options") if isinstance(provider, dict) else None
                if isinstance(options, dict) and (options.get("headers") or api_key_retained(options.get("apiKey"))):
                    retained_auth.add(vendor)

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
                    if api_key_retained(options.get("apiKey")):
                        continue
                    options.pop("apiKey", None)
                    if vendor not in retained_auth:
                        # A retained same-vendor credential, in auth.json or in
                        # any layer's config, keeps its endpoint.
                        options.pop("baseURL", None)
                    if not options:
                        provider.pop("options", None)

        for path in opencode_config_paths(home, project_roots):
            edit_json(path, clear_providers if clean_api_keys else _keep, jsonc=True, guard_unchanged=True)

        def clear_provider_auth(payload: dict) -> None:
            for vendor in vendors:
                if vendor not in payload or auth_entry_retained(payload[vendor]):
                    continue
                payload.pop(vendor, None)

        edit_json(
            opencode_auth_path(home), clear_provider_auth if clean_api_keys else _keep,
            guard_unchanged=True,
        )
    if backends and _include_shell:
        from .migration_shell import cleanup_shell_profile, read_shell_profiles

        selected_values: dict[str, str] = {}
        for item in items if clean_api_keys else ():
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
