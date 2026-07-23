"""Helpers for writing Codex's on-disk auth configuration.

The Codex CLI / ``codex app-server`` reads two files at launch time:

- ``~/.codex/config.toml`` — model + provider preferences (including the
  ``model_provider`` selector and the ``[model_providers.<id>]`` table that
  carries ``base_url``).
- ``~/.codex/auth.json`` — credential bag; the ``OPENAI_API_KEY`` field is
  the one Codex consumes for API-key mode.

This module mediates writes to those files so the Settings → Backends →
Codex UI can flip between OAuth (ChatGPT login) and API-key modes without
the user dropping into a terminal. The persistent app-server picks up
changes via ``restart_backend('codex')``.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# A TOML "bare key" is the unquoted form — anything outside this character
# set must be emitted as a quoted key. Codex specifically uses quoted keys
# under ``[projects."/absolute/path"]`` to scope per-directory settings,
# so the emitter has to round-trip those correctly.
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Provider id we manage in ``[model_providers.<id>]``. Newer Codex
# versions reserve ``openai`` as a built-in (the CLI refuses to load a
# config that overrides it: "model_providers contains reserved built-in
# provider IDs: openai"), so our managed section uses a non-reserved
# suffix. ``openai`` is kept as a legacy-cleanup target — see
# ``LEGACY_MANAGED_PROVIDER_IDS`` and the migration in ``apply_codex_auth``.
MANAGED_PROVIDER_ID = "openai-managed"
LEGACY_MANAGED_PROVIDER_IDS = ("openai",)

# Codex's top-level ``cli_auth_credentials_store`` controls where the CLI
# reads/writes cached credentials: ``file`` → ``~/.codex/auth.json``,
# ``keyring`` → OS keychain, ``auto`` → keyring-preferred. The Settings
# UI manages key material through ``auth.json`` exclusively (we have no
# cross-platform keyring backend), so we pin this to ``file`` whenever
# we write an API key — otherwise Codex would silently look in the
# keychain and behave as if no key was configured.
CREDENTIALS_STORE_KEY = "cli_auth_credentials_store"
CREDENTIALS_STORE_FILE = "file"


def get_codex_home(home: Path | None = None) -> Path:
    """Resolve the directory Codex actually reads ``config.toml`` from.

    Codex respects the ``CODEX_HOME`` environment variable (unlike most
    tools, this points directly at the data directory — *not* HOME).
    ``modules/agents/codex/agent.py`` already treats it as authoritative,
    so we mirror that here; otherwise "Save and restart Codex" can report
    success while the live process keeps reading a different directory.
    """
    if home is not None:
        return home / ".codex"
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".codex"


def get_codex_config_paths(home: Path | None = None) -> tuple[Path, Path]:
    """Return ``(config.toml, auth.json)`` paths under ``~/.codex``."""
    codex_home = get_codex_home(home)
    return codex_home / "config.toml", codex_home / "auth.json"


def _load_toml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        try:
            import tomllib  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - py<3.11 fallback
            import tomli as tomllib  # type: ignore[no-redef]
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Codex config.toml parse failed (%s); rewriting from empty", exc)
        return {}


def _load_auth(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning("Codex auth.json parse failed (%s); rewriting from empty", exc)
    return {}


def _format_toml_key(key: str) -> str:
    """Quote a TOML key when it falls outside the bare-key character class.

    Plain identifier keys like ``model_provider`` round-trip as-is; keys
    that contain dots, slashes, or other characters (most notably the
    absolute paths Codex uses under ``[projects.<...>]``) must be emitted
    as quoted strings so the resulting TOML stays parseable.
    """
    if _BARE_KEY_RE.match(key):
        return key
    return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _format_toml_header(path: Tuple[str, ...]) -> str:
    return "[" + ".".join(_format_toml_key(part) for part in path) + "]"


def _format_toml_array_header(path: Tuple[str, ...]) -> str:
    return "[[" + ".".join(_format_toml_key(part) for part in path) + "]]"


def _is_table_array(value: Any) -> bool:
    """A non-empty list whose items are all dicts is a TOML array-of-tables."""
    return isinstance(value, list) and bool(value) and all(isinstance(item, dict) for item in value)


def _dump_toml_inline_table(data: Dict[str, Any]) -> str:
    """Emit a dict as a TOML inline table: ``{ key = value, key2 = value2 }``.

    Used for dict elements that appear inside arrays — TOML calls these
    "inline tables" and they must stay single-line. Without this path,
    ``_dump_toml_value`` would fall through to ``json.dumps`` and write
    something like ``"{\\"name\\": \\"bar\\"}"`` — a quoted JSON string,
    not a table — silently corrupting valid configs such as
    ``contributors = ["foo", { name = "bar" }]``.
    """
    if not data:
        return "{}"
    parts = [f"{_format_toml_key(k)} = {_dump_toml_value(v)}" for k, v in data.items()]
    return "{ " + ", ".join(parts) + " }"


def _dump_toml_value(value: Any) -> str:
    """Serialize a single scalar value back to TOML. Tables handled separately."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    # TOML temporal scalars: ``tomllib`` parses ``2024-01-15T09:30:00`` as a
    # ``datetime``, dates as ``date``, times as ``time``. Their ``isoformat``
    # output is exactly the RFC3339-ish form TOML expects, and they are
    # emitted *unquoted* (a quoted version would round-trip as a string,
    # silently corrupting the user's config).
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, _dt.time):
        return value.isoformat()
    if isinstance(value, dict):
        # Pure dict-only lists are routed through ``[[a.b]]`` array-of-
        # tables emission in ``_dump_toml_table``, so reaching this branch
        # means we're inside a mixed array (or an explicit inline-table
        # value) — both of which TOML requires to stay single-line.
        return _dump_toml_inline_table(value)
    if isinstance(value, list):
        return "[" + ", ".join(_dump_toml_value(item) for item in value) + "]"
    # Fallback: serialize as JSON-ish string (best-effort for unexpected types).
    return _dump_toml_value(json.dumps(value))


def _dump_toml_table(data: Dict[str, Any], path: Tuple[str, ...], lines: List[str]) -> None:
    """Render *data* as a TOML table rooted at *path*, recursing into subtables.

    The split between scalars / subtables / array-of-tables mirrors what
    ``tomllib`` parses, so the rewrite is loss-less for arbitrary
    Codex-shaped configs:

    - scalar leaves under this path are emitted first, under the
      ``[path]`` header (or at the top of the file when ``path`` is empty);
    - dict children become standalone ``[path.subkey]`` tables, recursed
      into so deeper nesting like ``[a.b.c]`` round-trips;
    - lists of dicts become ``[[path.subkey]]`` array-of-tables entries.
    """
    scalars: List[Tuple[str, Any]] = []
    subtables: List[Tuple[str, Dict[str, Any]]] = []
    table_arrays: List[Tuple[str, List[Dict[str, Any]]]] = []
    for key, value in data.items():
        if isinstance(value, dict):
            subtables.append((key, value))
        elif _is_table_array(value):
            table_arrays.append((key, value))
        else:
            scalars.append((key, value))

    if path:
        # Emit ``[path]`` when this table has its own scalars, or when it
        # is otherwise empty (no children) — without the header, an empty
        # leaf disappears entirely from the round-trip. Pure container
        # tables (no scalars, but with subtables) are implicit in TOML:
        # ``[a.b]`` is enough to introduce ``a``.
        if scalars or (not subtables and not table_arrays):
            if lines:
                lines.append("")
            lines.append(_format_toml_header(path))
            for key, value in scalars:
                lines.append(f"{_format_toml_key(key)} = {_dump_toml_value(value)}")
    else:
        for key, value in scalars:
            lines.append(f"{_format_toml_key(key)} = {_dump_toml_value(value)}")

    for key, value in subtables:
        _dump_toml_table(value, path + (key,), lines)

    for key, items in table_arrays:
        sub_path = path + (key,)
        for item in items:
            if lines:
                lines.append("")
            lines.append(_format_toml_array_header(sub_path))
            item_scalars: List[Tuple[str, Any]] = []
            item_subtables: List[Tuple[str, Dict[str, Any]]] = []
            for ik, iv in item.items():
                if isinstance(iv, dict):
                    item_subtables.append((ik, iv))
                else:
                    item_scalars.append((ik, iv))
            for ik, iv in item_scalars:
                lines.append(f"{_format_toml_key(ik)} = {_dump_toml_value(iv)}")
            for ik, iv in item_subtables:
                _dump_toml_table(iv, sub_path + (ik,), lines)


def _dump_toml(data: Dict[str, Any]) -> str:
    """Emit *data* as TOML.

    Comments and original key ordering are lost (Python dicts preserve
    insertion order, so the rewrite is stable round-trip for a single
    parse → mutate → re-emit cycle). Everything else — quoted keys,
    arbitrary nesting depth, arrays of tables — is preserved so saving
    Codex auth never silently drops unrelated config blocks.
    """
    lines: List[str] = []
    _dump_toml_table(data, (), lines)
    return "\n".join(lines) + ("\n" if lines else "")


def _atomic_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
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


def apply_codex_auth(
    *,
    auth_mode: str,
    api_key: Optional[str],
    base_url: Optional[str],
    home: Path | None = None,
) -> Dict[str, Any]:
    """Persist the requested auth mode into Codex's on-disk config files.

    - ``api_key`` mode: write ``OPENAI_API_KEY`` into ``auth.json``,
      optionally set ``[model_providers.openai-managed].base_url`` if a
      non-default URL was supplied, and pin top-level ``model_provider``
      to the managed entry so Codex actually uses the keyed provider.
    - ``oauth`` mode: drop ``OPENAI_API_KEY`` from ``auth.json``, leave any
      ``tokens`` blob in place, and clear our managed ``base_url`` so the
      next launch goes back to OpenAI's default endpoint.

    Returns ``{"notices": [{code, ...}, ...]}`` — non-fatal warnings the
    caller may want to surface in the UI (e.g. "we cleared a custom
    relay pointer that won't accept OAuth tokens"). An empty list means
    the save was a no-op transformation.
    """
    if auth_mode not in {"oauth", "api_key"}:
        raise ValueError(f"Unsupported codex auth_mode: {auth_mode!r}")

    config_path, auth_path = get_codex_config_paths(home)
    auth_data = _load_auth(auth_path)
    toml_data = _load_toml(config_path)
    notices: list[Dict[str, Any]] = []

    providers = toml_data.setdefault("model_providers", {})
    if not isinstance(providers, dict):
        providers = {}
        toml_data["model_providers"] = providers
    managed = providers.setdefault(MANAGED_PROVIDER_ID, {})
    if not isinstance(managed, dict):
        managed = {}
        providers[MANAGED_PROVIDER_ID] = managed

    if auth_mode == "api_key":
        if not api_key:
            raise ValueError("api_key is required when auth_mode='api_key'")
        auth_data["OPENAI_API_KEY"] = api_key
        # Codex routes auth via ``auth.json``'s ``auth_mode`` field:
        # ``"apikey"`` → use OPENAI_API_KEY, ``"chatgpt"`` → use
        # ``tokens`` (OAuth). Without this line, a user who previously
        # signed in via OAuth keeps ``auth_mode = "chatgpt"`` on disk
        # and Codex sends their old ChatGPT bearer to the configured
        # base_url despite our managed API key being present — a
        # custom relay rejects it as ``INVALID_API_KEY``.
        auth_data["auth_mode"] = "apikey"
        # Drop the OAuth blob: the user explicitly chose API key, so
        # the ChatGPT tokens are stale and shouldn't linger in the file
        # where ``codex login`` would treat them as live. ``codex login
        # --with-api-key`` itself wipes these on switch — match that.
        auth_data.pop("tokens", None)
        auth_data.pop("last_refresh", None)
        # Only steer ``model_provider`` to our managed entry when the
        # field is unset or still points at one of our legacy / current
        # managed names. If the user has aimed it at a hand-rolled
        # provider (e.g. their ``[model_providers.OpenAI]`` relay
        # section), keep their pointer — overriding it would silently
        # bypass their custom base_url + wire_api config.
        current_mp = toml_data.get("model_provider")
        managed_known = {MANAGED_PROVIDER_ID, *LEGACY_MANAGED_PROVIDER_IDS, ""}
        if not isinstance(current_mp, str) or current_mp in managed_known:
            toml_data["model_provider"] = MANAGED_PROVIDER_ID
        # Pin Codex to file-based credentials so it actually reads the
        # ``OPENAI_API_KEY`` we just wrote. Without this, the documented
        # default (``auto``) prefers the OS keychain, and Codex would
        # behave as if no key was configured even though ``auth.json``
        # has one. See CREDENTIALS_STORE_KEY for the rationale.
        toml_data[CREDENTIALS_STORE_KEY] = CREDENTIALS_STORE_FILE
        managed.setdefault("name", "OpenAI")
        # Match the wire_api the modern Codex CLI uses internally for
        # api_key requests. Without explicitly setting this, the CLI
        # may fall back to the legacy ``chat`` shape — fine against
        # ``api.openai.com`` (which serves both endpoints) but the
        # common custom relays (e.g. ai-relay.chainbot.io) only speak
        # the Responses API and return 404 / wire-shape errors.
        managed.setdefault("wire_api", "responses")
        # Ensure the Bearer header is sent when the request travels
        # through the user's relay. The default is reasonable but
        # being explicit avoids a class of failures where Codex omits
        # auth on custom providers (silent 401 on the relay).
        managed.setdefault("requires_openai_auth", True)
        if base_url:
            managed["base_url"] = base_url
            # Custom relays almost never speak Codex's bespoke responses-
            # over-WebSocket protocol — they reverse-proxy HTTPS to OpenAI
            # but don't accept the WSS upgrade Codex expects on
            # ``/responses``. Codex 0.130+ gates that transport on this
            # field (``codex-rs/core/src/client.rs::responses_websocket_enabled``);
            # pinning it to false routes turns through the HTTP responses
            # path, which honors our ``base_url``. Leaving it absent lets
            # newer Codex versions dispatch WSS via the built-in OpenAI
            # provider's default (``wss://api.openai.com/v1/responses``),
            # silently bypassing the user's relay and producing 401s on
            # whatever account-bound key the relay handed out.
            managed["supports_websockets"] = False
        else:
            managed.pop("base_url", None)
            # No custom base_url → user is on the built-in OpenAI endpoint
            # where WSS works the way Codex expects. Drop our override so
            # Codex's own default-on behavior applies.
            managed.pop("supports_websockets", None)
    else:  # oauth
        auth_data.pop("OPENAI_API_KEY", None)
        # Flip auth_mode back to chatgpt when OAuth tokens still exist
        # on disk — otherwise Codex sees a stale ``"apikey"`` value and
        # rejects the request even though tokens are present. If no
        # tokens are around (e.g. user removed the key without ever
        # signing in to OAuth), clear ``auth_mode`` so the CLI prompts
        # for ``codex login`` rather than failing opaquely.
        if isinstance(auth_data.get("tokens"), dict) and auth_data["tokens"]:
            auth_data["auth_mode"] = "chatgpt"
        else:
            auth_data.pop("auth_mode", None)
        # Leave cli_auth_credentials_store as-is — switching back to
        # ChatGPT/OAuth is the user's responsibility via ``codex login``
        # (which may legitimately want keyring storage); we just stop
        # pinning the keyed provider's overrides.
        managed.pop("base_url", None)
        # Drop the managed section entirely on oauth — the api_key path
        # is the only thing that needs the ``name = "OpenAI"`` stub, and
        # leaving it behind under the legacy reserved name (``openai``)
        # makes newer Codex versions refuse to load the config at all.
        providers.pop(MANAGED_PROVIDER_ID, None)
        current_mp = toml_data.get("model_provider")
        managed_known = {MANAGED_PROVIDER_ID, *LEGACY_MANAGED_PROVIDER_IDS}
        if isinstance(current_mp, str) and current_mp in managed_known:
            # Revert top-level ``model_provider`` if we own it.
            toml_data.pop("model_provider", None)
        elif isinstance(current_mp, str) and current_mp:
            # User-owned pointer (e.g. a TitleCase ``[model_providers.OpenAI]``
            # relay block). OAuth tokens are issued by ``auth.openai.com``
            # and only validated by OpenAI's official Responses endpoint —
            # a custom relay almost never accepts them and returns
            # ``401 INVALID_API_KEY``. If the pointed section has a
            # ``base_url``, clear the pointer so Codex falls back to its
            # built-in ``openai`` provider with the default endpoint.
            # The section itself stays untouched, so switching back to
            # api_key mode can manually re-point at the relay if the user
            # wants.
            ptr_section = providers.get(current_mp)
            if isinstance(ptr_section, dict):
                ptr_base = ptr_section.get("base_url")
                if isinstance(ptr_base, str) and ptr_base.strip():
                    toml_data.pop("model_provider", None)
                    notices.append(
                        {
                            "code": "cleared_custom_relay_pointer",
                            "provider_id": current_mp,
                            "base_url": ptr_base.strip(),
                        }
                    )

    # Always purge any legacy reserved-name section we may have written
    # in older releases. Codex 1.x refuses to load a config that defines
    # ``[model_providers.openai]`` ("Built-in providers cannot be
    # overridden"); leaving it around bricks the CLI for the user.
    for legacy_id in LEGACY_MANAGED_PROVIDER_IDS:
        providers.pop(legacy_id, None)
    if not providers:
        toml_data.pop("model_providers", None)

    _atomic_write(auth_path, json.dumps(auth_data, indent=2) + "\n", mode=0o600)
    _atomic_write(config_path, _dump_toml(toml_data), mode=0o600)
    return {"notices": notices}


def read_codex_api_key(home: Path | None = None) -> Optional[str]:
    """Return the API key currently stored in ``auth.json``, if any.

    Used as a fallback when the UI sends a base-URL-only update: the
    V2Config cache may not have the key (e.g. ``codex login --with-api-key``
    wrote it directly to ``auth.json`` outside our flow), but the live
    Codex process still reads it from disk, so we must too.
    """
    _, auth_path = get_codex_config_paths(home)
    raw = _load_auth(auth_path).get("OPENAI_API_KEY")
    if isinstance(raw, str) and raw.strip():
        return raw
    return None


def _extract_chatgpt_account(auth_data: dict) -> Optional[Dict[str, Any]]:
    """Best-effort decode of the ChatGPT identity stored in ``auth.json``.

    Codex stores the OAuth bundle as ``{"tokens": {"id_token": "<JWT>",
    ...}}``. The id_token's payload carries an ``email`` and a nested
    ChatGPT-specific block with plan + org info. Decoding is signature-
    free (we don't verify it — these are tokens we received from
    ``codex login`` and we trust them at-rest); failures fall back to
    ``None`` so the Settings UI just shows the generic "signed in"
    banner instead of crashing.
    """
    tokens = auth_data.get("tokens") if isinstance(auth_data, dict) else None
    if not isinstance(tokens, dict):
        return None
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str):
        return None
    parts = id_token.split(".")
    if len(parts) < 2:
        return None
    payload_b64 = parts[1]
    # Pad base64 to a multiple of 4 (JWTs strip ``=`` padding).
    pad = "=" * (-len(payload_b64) % 4)
    try:
        import base64

        decoded = base64.urlsafe_b64decode((payload_b64 + pad).encode("ascii"))
        claims = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(claims, dict):
        return None

    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    name = claims.get("name") if isinstance(claims.get("name"), str) else None
    chatgpt = claims.get("https://api.openai.com/auth")
    plan_type: Optional[str] = None
    organizations: list[Dict[str, Any]] = []
    if isinstance(chatgpt, dict):
        if isinstance(chatgpt.get("chatgpt_plan_type"), str):
            plan_type = chatgpt["chatgpt_plan_type"]
        orgs_raw = chatgpt.get("organizations")
        if isinstance(orgs_raw, list):
            for entry in orgs_raw:
                if not isinstance(entry, dict):
                    continue
                organizations.append(
                    {
                        "id": entry.get("id") if isinstance(entry.get("id"), str) else None,
                        "title": entry.get("title") if isinstance(entry.get("title"), str) else None,
                        "role": entry.get("role") if isinstance(entry.get("role"), str) else None,
                        "is_default": bool(entry.get("is_default")),
                    }
                )
    return {
        "email": email,
        "name": name,
        "plan_type": plan_type,
        "organizations": organizations or None,
    }


def read_codex_auth_state(home: Path | None = None) -> Dict[str, Any]:
    """Return the user-visible auth state for the Settings UI.

    Reads both files and reports back what the user would see — no
    secrets in the response (the UI receives the key length, never the
    plaintext key).

    ``credentials_store`` reflects Codex's current ``cli_auth_credentials_store``
    setting; when it is not ``"file"``, the live key may live in the OS
    keychain and ``has_api_key`` is a file-only signal. Callers that
    need to surface the "we can't see your key, it's in the keyring"
    case should branch on this field rather than treating
    ``has_api_key=false`` as definitive.
    """
    config_path, auth_path = get_codex_config_paths(home)
    auth_data = _load_auth(auth_path)
    toml_data = _load_toml(config_path)
    api_key = auth_data.get("OPENAI_API_KEY")
    has_chatgpt_tokens = isinstance(auth_data.get("tokens"), dict)
    chatgpt_account = _extract_chatgpt_account(auth_data) if has_chatgpt_tokens else None

    providers = toml_data.get("model_providers")
    base_url: Optional[str] = None
    wire_api: Optional[str] = None
    if isinstance(providers, dict):
        # Codex's runtime selects the provider named by top-level
        # ``model_provider``. When that's a user-defined section (e.g.
        # ``[model_providers.OpenAI]`` for a relay), our managed-id
        # lookup would miss the user's actual ``base_url``. Prefer the
        # active provider's section; fall back to the managed id we
        # ourselves write so the UI still reflects a vibe-initiated
        # save before the user customises ``config.toml`` by hand.
        active_provider = toml_data.get("model_provider")
        active_section: Optional[dict] = None
        if isinstance(active_provider, str) and isinstance(providers.get(active_provider), dict):
            active_section = providers[active_provider]
        elif isinstance(providers.get(MANAGED_PROVIDER_ID), dict):
            active_section = providers[MANAGED_PROVIDER_ID]
        else:
            # Legacy fallback: older releases wrote our managed shape
            # under ``[model_providers.openai]``. New Codex rejects that
            # name as reserved, so we'll purge it on the next save, but
            # the UI should still surface the base_url until then.
            for legacy_id in LEGACY_MANAGED_PROVIDER_IDS:
                legacy_section = providers.get(legacy_id)
                if isinstance(legacy_section, dict):
                    active_section = legacy_section
                    break
        if isinstance(active_section, dict):
            raw = active_section.get("base_url")
            if isinstance(raw, str) and raw.strip():
                base_url = raw.strip()
            raw_wire_api = active_section.get("wire_api")
            if isinstance(raw_wire_api, str) and raw_wire_api.strip():
                wire_api = raw_wire_api.strip()

    store_raw = toml_data.get(CREDENTIALS_STORE_KEY)
    credentials_store = store_raw if isinstance(store_raw, str) else None
    # Codex's default when the key is absent is ``auto`` (keyring-preferred);
    # report that explicitly so the UI doesn't have to know the default.
    effective_store = credentials_store or "auto"
    file_store_active = effective_store == CREDENTIALS_STORE_FILE

    inferred_mode = "api_key" if isinstance(api_key, str) and api_key else "oauth"
    # When Codex is configured to use the OS keychain (``auto`` /
    # ``keyring``) and ``auth.json`` carries no key and no ChatGPT tokens,
    # we genuinely cannot tell whether the user is in api_key mode (key
    # in keychain) or oauth/not-signed-in. ``has_api_key`` is a file-only
    # signal in that case; callers must treat ``auth_mode`` as a best
    # guess rather than the truth and surface ``auth_mode_uncertain`` so
    # the UI can say "we can't read your auth here" instead of "no key".
    auth_mode_uncertain = (
        not file_store_active and not (isinstance(api_key, str) and api_key) and not has_chatgpt_tokens
    )
    return {
        "auth_mode": inferred_mode,
        "has_api_key": isinstance(api_key, str) and bool(api_key),
        "api_key_length": len(api_key) if isinstance(api_key, str) else 0,
        # Plaintext API key — only consumed by ``vibe.api.get_codex_auth``
        # which masks it before returning to the UI. Never serialized to
        # JSON directly.
        "api_key_raw": api_key if isinstance(api_key, str) and api_key else None,
        "base_url": base_url,
        "wire_api": wire_api,
        "has_chatgpt_tokens": has_chatgpt_tokens,
        # ``chatgpt_account``: best-effort identity from the OAuth JWT in
        # ``auth.json`` so the Settings page can show "Signed in as
        # gpt1@example.com (Pro)" instead of just "ChatGPT credentials
        # detected". Returns ``None`` when no JWT is available or the
        # claims are missing — the UI degrades to the generic banner.
        "chatgpt_account": chatgpt_account,
        "credentials_store": effective_store,
        "file_store_active": file_store_active,
        "auth_mode_uncertain": auth_mode_uncertain,
    }
