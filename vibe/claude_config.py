"""Helpers for reading and writing Claude Code's on-disk configuration.

Claude Code applies the ``env`` block in ``~/.claude/settings.json`` when it
launches, and that block wins over inherited process environment. avibe
therefore treats that file as the source of truth for Claude API-key auth
instead of storing secrets in V2Config and hoping env injection wins.

This module owns the narrow settings.json mutation surface:

- ``apply_claude_auth(...)`` upserts or removes Anthropic env vars in
  ``settings.json`` while preserving Avibe-managed Claude Code defaults.
- ``read_claude_settings_env(...)`` reports the live on-disk state without
  leaking secrets beyond the current process.

OAuth tokens minted by ``claude login`` may live in an OS keychain or in
Claude Code's own credential file depending on platform/version. The Settings
UI prefers ``claude auth status --json`` and uses the on-disk credential file
as a fallback signal.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Keys Avibe always keeps in ``~/.claude/settings.json`` for Claude Code.
# They are not auth credentials, so they intentionally stay outside
# ``RELEVANT_ENV_KEYS`` and survive OAuth/API-key mode switches.
MANAGED_ENV_VALUES = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
}

# Keys we recognise inside ``~/.claude/settings.json``'s ``env`` block.
# ``ANTHROPIC_API_KEY`` is the SDK's documented variable; relay setups
# (e.g. Cloudflare-fronted gateways like ai-relay) prefer
# ``ANTHROPIC_AUTH_TOKEN``. We surface both so a hand-edited config
# doesn't silently invalidate the Settings UI.
RELEVANT_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)
OAUTH_SETTINGS_ENV_BACKUP_NAME = ".avibe-oauth-settings-env-backup.json"


def get_claude_home(home: Path | None = None) -> Path:
    """Resolve the directory Claude Code reads ``settings.json`` from.

    Claude Code respects ``CLAUDE_CONFIG_DIR`` first (newer builds),
    falling back to ``~/.claude``. We mirror that precedence so the
    Settings UI reports on whichever directory the live CLI actually
    consults.
    """
    if home is not None:
        return home / ".claude"
    env_home = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".claude"


def get_claude_settings_path(home: Path | None = None) -> Path:
    """Return the absolute path to ``~/.claude/settings.json``."""
    return get_claude_home(home) / "settings.json"


def get_claude_credentials_paths(home: Path | None = None) -> tuple[Path, ...]:
    """Return Claude Code OAuth credential paths, newest layout first.

    Claude Code currently writes OAuth tokens to
    ``~/.claude/.credentials.json`` on Linux/Docker. Older Avibe builds
    looked for ``credentials.json`` based on earlier CLI behavior, so keep
    both paths readable for existing installs and tests.
    """
    claude_home = get_claude_home(home)
    return (
        claude_home / ".credentials.json",
        claude_home / "credentials.json",
    )


def get_claude_credentials_path(home: Path | None = None) -> Path:
    """Return the primary Claude Code OAuth credentials path."""
    return get_claude_credentials_paths(home)[0]


def clear_claude_oauth_credentials_files(home: Path | None = None) -> list[str]:
    """Remove known file-backed Claude Code OAuth credential stores."""
    removed: list[str] = []
    for path in get_claude_credentials_paths(home):
        try:
            path.unlink()
            removed.append(str(path))
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise OSError(f"Failed to remove Claude OAuth credentials file {path}: {exc}") from exc
    return removed


def get_claude_oauth_settings_backup_path(home: Path | None = None) -> Path:
    """Return Avibe's durable rollback file for Claude OAuth setup."""
    return get_claude_home(home) / OAUTH_SETTINGS_ENV_BACKUP_NAME


def read_claude_oauth_signed_in(home: Path | None = None) -> bool:
    """Best-effort probe for whether Claude has a usable OAuth session.

    True iff Claude's on-disk credentials file exists and carries something
    that looks like an OAuth token bundle. We don't attempt to introspect
    keychain-backed installs (macOS) — those return False here but the UI
    can still light up the OAuth banner after a successful in-app login.
    """
    for path in get_claude_credentials_paths(home):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        # Claude writes nested ``claudeAiOauth`` payloads in newer builds; flat
        # ``access_token``/``refresh_token`` keys also occur. Accept either.
        nested = data.get("claudeAiOauth") if isinstance(data.get("claudeAiOauth"), dict) else None
        if nested and any(
            isinstance(nested.get(field), str) and nested.get(field)
            for field in ("access_token", "refresh_token", "accessToken", "refreshToken")
        ):
            return True
        for field in ("access_token", "refresh_token", "accessToken", "refreshToken"):
            if isinstance(data.get(field), str) and data.get(field):
                return True
    return False


def _load_settings(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning("Claude settings.json parse failed (%s)", exc)
    return {}


def _load_settings_for_write(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Claude settings.json must contain a JSON object")
    return data


def _atomic_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover - best effort cleanup
                pass
    try:
        path.chmod(mode)
    except OSError as exc:  # pragma: no cover - non-POSIX
        logger.debug("chmod %s failed: %s", path, exc)


def _clean_relevant_env_values(env_values: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key in RELEVANT_ENV_KEYS:
        raw = env_values.get(key)
        if isinstance(raw, str) and raw.strip():
            out[key] = raw.strip()
    return out


def _ensure_managed_env_values(env_block: Dict[str, Any]) -> None:
    """Upsert Avibe-managed, non-secret Claude Code env defaults."""
    env_block.update(MANAGED_ENV_VALUES)


def write_claude_oauth_settings_backup(
    env_values: Dict[str, str], home: Path | None = None
) -> None:
    """Persist a rollback copy of Claude settings env before OAuth cleanup.

    OAuth setup has to remove API-key/base-url overrides from Claude Code's
    ``settings.json`` before launching the OAuth control client. The rollback
    state must survive an Avibe process restart, so it lives next to Claude's
    settings file and uses the same private-file permissions.
    """
    cleaned = _clean_relevant_env_values(env_values)
    if not cleaned:
        clear_claude_oauth_settings_backup(home)
        return
    payload = {"version": 1, "env": cleaned}
    _atomic_write(
        get_claude_oauth_settings_backup_path(home),
        json.dumps(payload, indent=2) + "\n",
        mode=0o600,
    )


def read_claude_oauth_settings_backup(
    home: Path | None = None,
) -> Dict[str, str] | None:
    """Read Avibe's pending Claude OAuth rollback backup, if present."""
    path = get_claude_oauth_settings_backup_path(home)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Claude OAuth settings backup read failed (%s)", exc)
        return None
    if not isinstance(data, dict):
        return None
    env_values = data.get("env")
    if not isinstance(env_values, dict):
        return None
    cleaned = _clean_relevant_env_values(env_values)
    return cleaned or None


def clear_claude_oauth_settings_backup(home: Path | None = None) -> None:
    """Remove Avibe's pending Claude OAuth rollback backup."""
    path = get_claude_oauth_settings_backup_path(home)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:  # pragma: no cover - best effort cleanup
        logger.warning("Claude OAuth settings backup cleanup failed (%s)", exc)


def apply_claude_auth(
    *,
    auth_mode: str,
    api_key: Optional[str],
    base_url: Optional[str],
    auth_token: Optional[str] = None,
    home: Path | None = None,
) -> Dict[str, Any]:
    """Persist Claude auth into ``settings.json``.

    ``api_key`` mode writes ``env.ANTHROPIC_API_KEY`` and removes
    ``ANTHROPIC_AUTH_TOKEN`` so header semantics cannot conflict. ``oauth``
    mode removes all Anthropic credential/base-url overrides from the env
    block and leaves Claude's OAuth credentials untouched. Both modes upsert
    Avibe's non-secret Claude Code env defaults.
    """
    if auth_mode not in {"oauth", "api_key"}:
        raise ValueError(f"Unsupported claude auth_mode: {auth_mode!r}")
    if auth_mode == "api_key" and not (api_key or auth_token):
        raise ValueError("api_key is required when auth_mode='api_key'")

    path = get_claude_settings_path(home)
    settings = _load_settings_for_write(path)
    env_block = settings.setdefault("env", {})
    if not isinstance(env_block, dict):
        env_block = {}
        settings["env"] = env_block

    if auth_mode == "api_key":
        if api_key:
            env_block["ANTHROPIC_API_KEY"] = api_key.strip()
            env_block.pop("ANTHROPIC_AUTH_TOKEN", None)
        elif auth_token:
            env_block["ANTHROPIC_AUTH_TOKEN"] = auth_token.strip()
            env_block.pop("ANTHROPIC_API_KEY", None)
        if base_url:
            env_block["ANTHROPIC_BASE_URL"] = base_url.strip()
        else:
            env_block.pop("ANTHROPIC_BASE_URL", None)
    else:
        for key in RELEVANT_ENV_KEYS:
            env_block.pop(key, None)

    _ensure_managed_env_values(env_block)

    _atomic_write(path, json.dumps(settings, indent=2) + "\n", mode=0o600)
    return {"settings_path": str(path)}


def read_claude_settings_env(home: Path | None = None) -> Dict[str, str]:
    """Extract Anthropic-relevant env vars from ``~/.claude/settings.json``.

    Returns the mapping verbatim (no length truncation, no masking) so
    callers can compute presence + length. The caller is responsible for
    redacting before forwarding to the UI.
    """
    settings = _load_settings(get_claude_settings_path(home))
    env_block = settings.get("env")
    if not isinstance(env_block, dict):
        return {}
    out: Dict[str, str] = {}
    for key in RELEVANT_ENV_KEYS:
        raw = env_block.get(key)
        if isinstance(raw, str) and raw.strip():
            out[key] = raw.strip()
    return out


def restore_claude_settings_env(env_values: Dict[str, str], home: Path | None = None) -> None:
    """Restore Anthropic-relevant Claude settings env values exactly.

    OAuth setup temporarily removes these keys so Claude Code cannot route
    the OAuth handshake through stale API-key settings. If that setup does
    not complete, the caller needs to put the previous settings back without
    changing header semantics or dropping a base-url-only relay setting.
    """
    path = get_claude_settings_path(home)
    settings = _load_settings_for_write(path)
    env_block = settings.setdefault("env", {})
    if not isinstance(env_block, dict):
        env_block = {}
        settings["env"] = env_block

    for key in RELEVANT_ENV_KEYS:
        env_block.pop(key, None)
        raw = env_values.get(key)
        if isinstance(raw, str) and raw.strip():
            env_block[key] = raw.strip()

    _ensure_managed_env_values(env_block)

    if not env_block:
        settings.pop("env", None)

    _atomic_write(path, json.dumps(settings, indent=2) + "\n", mode=0o600)


def read_claude_auth_state(home: Path | None = None) -> Dict[str, Any]:
    """Return the user-visible Claude auth state for the Settings UI.

    Reports on ``~/.claude/settings.json`` only — V2Config is layered on
    top by ``vibe.api.get_claude_auth``. Secrets never leave the server;
    we surface key length + a "settings.json conflict" flag.

    ``settings_env_has_key`` is true when settings.json carries either
    ``ANTHROPIC_API_KEY`` or ``ANTHROPIC_AUTH_TOKEN``. When this is true
    *and* V2Config also has a key, the Settings UI must warn that
    settings.json wins at launch (Claude Code applies its env block on
    top of whatever we inject). ``settings_path`` is forwarded so the
    warning can name the file the user needs to edit.
    """
    env_block = read_claude_settings_env(home)
    settings_path = get_claude_settings_path(home)

    settings_key = env_block.get("ANTHROPIC_API_KEY") or env_block.get(
        "ANTHROPIC_AUTH_TOKEN"
    )
    settings_base = env_block.get("ANTHROPIC_BASE_URL")
    return {
        "settings_path": str(settings_path),
        "settings_exists": settings_path.exists(),
        "settings_env_has_key": bool(settings_key),
        "settings_env_key_length": len(settings_key) if settings_key else 0,
        "settings_env_key_var": (
            "ANTHROPIC_API_KEY"
            if "ANTHROPIC_API_KEY" in env_block
            else ("ANTHROPIC_AUTH_TOKEN" if "ANTHROPIC_AUTH_TOKEN" in env_block else None)
        ),
        "settings_env_base_url": settings_base,
    }


def read_claude_api_key_from_settings(home: Path | None = None) -> Optional[str]:
    """Return ``settings.json``'s ``ANTHROPIC_API_KEY`` if it has one.

    Used as a fallback when the UI sends a base-URL-only update: V2Config
    may be stale (older installs lacked ``api_key``), but the CLI still
    picks up whatever is in ``settings.json``. Prefer that over silently
    blanking the live key.

    Restricted to ``ANTHROPIC_API_KEY`` on purpose. ``ANTHROPIC_AUTH_TOKEN``
    is the bearer-token relay variant — Claude Code applies it from
    ``settings.json`` directly, and our ``api_key`` field always injects
    ``ANTHROPIC_API_KEY`` at launch (see ``session_handler``). Pulling an
    auth-token value into V2Config.api_key would silently switch the
    header semantics on the next save and break bearer-token gateways.
    Bearer-token users should rely on the existing settings.json path or
    re-enter their key into the form.
    """
    env_block = read_claude_settings_env(home)
    return env_block.get("ANTHROPIC_API_KEY")


def build_claude_subprocess_env(
    claude_cfg: Any,
    base_env: Optional[Dict[str, str]] = None,
    *,
    force_oauth: bool = False,
) -> Dict[str, str]:
    """Build the env dict passed to every Claude subprocess / SDK client.

    Both ``core/handlers/session_handler.py`` (one-shot CLI launches) and
    ``core/agent_auth_service.py`` (control-channel SDK clients) need the
    same Anthropic/Claude env composition: inherit relevant vars from the
    parent process, then let explicit V2Config intent decide whether to
    strip stale inherited vars. API-key material itself now comes from
    Claude's own ``settings.json``; V2Config keys are legacy fallback only.

    The ``auth_mode`` toggle is the load-bearing piece — if a user picks
    OAuth in Settings but their shell exports ``ANTHROPIC_API_KEY``, the
    Claude CLI silently keeps API-key auth and never reaches
    Claude Code's OAuth credential store. Stripping both ``ANTHROPIC_API_KEY``
    and ``ANTHROPIC_AUTH_TOKEN`` (header-semantics switch) in OAuth mode
    makes the Settings toggle authoritative.

    ``force_oauth=True`` is the escape hatch for callers that ARE the
    OAuth setup flow (the control-channel SDK client in
    ``_start_claude_control_flow`` and the test probe's env builder).
    The flow itself is OAuth semantics by construction — inherited
    ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` / a relay
    ``ANTHROPIC_BASE_URL`` would route the OAuth handshake through an
    api-key gateway and break the login regardless of what
    ``auth_mode_set`` happens to say. Legacy installs on their first
    sign-in attempt would otherwise never see the strip — they haven't
    saved anything through Settings yet, so the marker is still
    ``False`` — and OAuth would fail silently against inherited API
    credentials.
    """

    env_source = base_env if base_env is not None else os.environ
    claude_env: Dict[str, str] = {}
    for key, value in env_source.items():
        if key.startswith("ANTHROPIC_") or key.startswith("CLAUDE_"):
            claude_env[key] = value

    if claude_cfg is None and not force_oauth:
        return claude_env

    configured_auth_mode = (
        getattr(claude_cfg, "auth_mode", "oauth") if claude_cfg is not None else "oauth"
    )
    auth_mode = "oauth" if force_oauth else configured_auth_mode
    configured_key_raw = (getattr(claude_cfg, "api_key", None) or "").strip() if claude_cfg is not None else ""
    configured_base = (getattr(claude_cfg, "base_url", None) or "").strip() if claude_cfg is not None else ""
    settings_env = read_claude_settings_env()
    settings_api_key = settings_env.get("ANTHROPIC_API_KEY") or ""
    settings_auth_token = settings_env.get("ANTHROPIC_AUTH_TOKEN") or ""
    settings_base = settings_env.get("ANTHROPIC_BASE_URL") or ""
    # ``auth_mode_set`` is False on V2 configs that predate the Settings
    # → Backends → Claude page (or that the user has simply never
    # opened). On those installs the user is running on shell-exported
    # ``ANTHROPIC_*`` vars and we must preserve them — stripping would
    # break working legacy deployments. Once the user saves any auth
    # choice through Settings (api_key save, OAuth save, Sign out,
    # Remove key), the writer flips this to True and we honor
    # ``auth_mode`` strictly.
    auth_mode_set = bool(getattr(claude_cfg, "auth_mode_set", False)) if claude_cfg is not None else False

    if auth_mode == "oauth":
        if auth_mode_set or force_oauth:
            # Explicit OAuth pick from the UI, OR a caller that knows
            # it IS the OAuth setup flow. Strip every inherited
            # Anthropic credential header: an ambient
            # ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` would
            # suppress Claude Code's OAuth credential store, and an ambient
            # ``ANTHROPIC_BASE_URL`` (typically a stale relay URL from
            # the shell) would route OAuth traffic through an
            # api-key-only gateway. Both leaks have to be plugged for
            # the "OAuth in Settings" promise to mean anything.
            claude_env.pop("ANTHROPIC_API_KEY", None)
            claude_env.pop("ANTHROPIC_AUTH_TOKEN", None)
            claude_env.pop("ANTHROPIC_BASE_URL", None)
        # else: legacy install — preserve inherited env vars verbatim.
    elif auth_mode == "api_key":
        if auth_mode_set:
            claude_env.pop("ANTHROPIC_API_KEY", None)
            claude_env.pop("ANTHROPIC_AUTH_TOKEN", None)
            claude_env.pop("ANTHROPIC_BASE_URL", None)
        if settings_api_key:
            claude_env["ANTHROPIC_API_KEY"] = settings_api_key
            claude_env.pop("ANTHROPIC_AUTH_TOKEN", None)
        elif settings_auth_token:
            claude_env["ANTHROPIC_AUTH_TOKEN"] = settings_auth_token
            claude_env.pop("ANTHROPIC_API_KEY", None)
        elif configured_key_raw:
            claude_env["ANTHROPIC_API_KEY"] = configured_key_raw
            claude_env.pop("ANTHROPIC_AUTH_TOKEN", None)

    effective_base = settings_base or configured_base
    if effective_base and auth_mode != "oauth":
        claude_env["ANTHROPIC_BASE_URL"] = effective_base

    return claude_env
