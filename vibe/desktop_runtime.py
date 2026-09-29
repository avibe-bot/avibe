"""Desktop-shell endpoint and listener contracts.

The desktop shell always connects through a literal loopback origin, even when
the user's primary UI bind is a specific LAN or overlay-network address.  The
UI process owns both listeners so the shell never has to interpret Avibe
configuration or broaden its navigation policy.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, TypedDict

DESKTOP_ENDPOINT_SCHEMA_VERSION: Literal[1] = 1
DESKTOP_RUNTIME_ID_ENV = "AVIBE_DESKTOP_RUNTIME_ID"
DESKTOP_RUNTIME_ROOT_ENV = "AVIBE_DESKTOP_RUNTIME_ROOT"
# The install marker the desktop host writes at the root of every private tree
# it unpacks: `INSTALL_MARKER_NAME` and `write_marker` in
# desktop/runtime-host/src/private_runtime.rs. Its `archive_sha256` is the id
# the launcher stamps as AVIBE_DESKTOP_RUNTIME_ID; its `python_entrypoint` is the
# tree-relative interpreter the launcher runs.
DESKTOP_RUNTIME_MARKER_NAME = ".avibe-runtime.json"
_DESKTOP_RUNTIME_MARKER_MAX_BYTES = 32 * 1024
# The directories between a tree root and its interpreter, one per
# `python_entrypoint` in desktop/runtime-sources.json: `python/bin/python3` and
# `python/python.exe`.
_DESKTOP_INTERPRETER_DIRS = (("python", "bin"), ("python",))
DESKTOP_NODE_BIN_ENV = "VIBE_SHOW_RUNTIME_NODE_BIN"
DESKTOP_NPM_CLI_ENV = "AVIBE_DESKTOP_NPM_CLI"
DESKTOP_BACKENDS_ROOT_ENV = "AVIBE_DESKTOP_BACKENDS_ROOT"
# Set by the desktop launcher on every Runtime it starts; no code branches on it.
DESKTOP_SHELL_ENV = "AVIBE_DESKTOP_SHELL"
# Every process a backend install starts carries this, so a process scan tells
# an installer tree from the other programs that carry the Runtime id.
DESKTOP_ROLE_ENV = "AVIBE_DESKTOP_ROLE"
DESKTOP_INSTALLER_ROLE = "installer"
# The OpenCode server manager stamps the server. The agent work it runs inherits
# the stamp too, so the stamp alone names no role.
DESKTOP_OPENCODE_ROLE = "opencode"
# ``<pid>:<create time>`` of the UI that started an installer tree, which every
# member inherits: the tree is abandoned once that UI is gone. A pid that is not
# alive is gone. macOS can shift the create time of a running process, so a
# different one is a reused pid only when the process holding it is readable and
# is not a UI of the tree's Runtime; when it cannot be read, nobody can tell.
DESKTOP_INSTALLER_OWNER_ENV = "AVIBE_DESKTOP_INSTALLER_OWNER"


class DesktopEndpointPayload(TypedDict):
    schema_version: Literal[1]
    origin: str


def _runtime_id_value(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 64:
        return None
    if any(character not in "0123456789abcdef" for character in value):
        return None
    return value


def desktop_runtime_id(base_env: Mapping[str, str] | None = None) -> str | None:
    """Return the validated identity of a desktop-managed Runtime."""

    env = os.environ if base_env is None else base_env
    return _runtime_id_value(env.get(DESKTOP_RUNTIME_ID_ENV, ""))


def _desktop_tree_runtime_id(executable: str | os.PathLike[str] | None) -> str | None:
    """The id of the private tree whose interpreter *executable* is, or None.

    Only the marker at the root of that tree counts: the directory the
    interpreter's own layout puts the root at, holding a valid
    `archive_sha256`, whose `python_entrypoint` leads back to this very
    interpreter. A marker anywhere else says nothing about who is running, and
    neither does one that fails any of these.
    """

    if not executable:
        return None
    try:
        interpreter = Path(executable).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    parents = interpreter.parents
    for dirs in _DESKTOP_INTERPRETER_DIRS:
        if len(parents) <= len(dirs) or tuple(parent.name for parent in parents[: len(dirs)]) != dirs[::-1]:
            continue
        root = parents[len(dirs)]
        marker = root / DESKTOP_RUNTIME_MARKER_NAME
        if os.path.lexists(marker):
            return _desktop_marker_runtime_id(root, marker, interpreter)
    return None


def _desktop_marker_runtime_id(root: Path, marker: Path, interpreter: Path) -> str | None:
    try:
        if marker.is_symlink() or not marker.is_file():
            return None
        with marker.open("rb") as handle:
            raw = handle.read(_DESKTOP_RUNTIME_MARKER_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _DESKTOP_RUNTIME_MARKER_MAX_BYTES:
        return None
    try:
        manifest = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(manifest, dict):
        return None
    runtime_id = _runtime_id_value(manifest.get("archive_sha256"))
    entrypoint = manifest.get("python_entrypoint")
    if runtime_id is None or not isinstance(entrypoint, str) or not entrypoint:
        return None
    if Path(entrypoint).is_absolute():
        return None
    try:
        if (root / entrypoint).resolve(strict=True) != interpreter:
            return None
    except (OSError, RuntimeError):
        return None
    return runtime_id


def desktop_caller_provenance() -> frozenset[str]:
    """The desktop Runtime ids this process can act for.

    A process carries the id the launcher stamped into its environment, and
    runs from the tree its interpreter was unpacked into; either one makes it a
    desktop caller. An empty set is a caller with no desktop provenance at all:
    a terminal user managing their own Avibe.
    """

    ids = {
        desktop_runtime_id(),
        _desktop_tree_runtime_id(sys.executable),
    }
    return frozenset(value for value in ids if value is not None)


def private_desktop_runtime_root(base_env: Mapping[str, str] | None = None) -> Path | None:
    """Return the app-private Runtime root supplied by the desktop launcher."""

    env = os.environ if base_env is None else base_env
    value = env.get(DESKTOP_RUNTIME_ROOT_ENV, "")
    if not value:
        return None
    root = Path(value).expanduser()
    if not root.is_absolute():
        return None
    return root.resolve(strict=False)


def private_desktop_node_bin(base_env: Mapping[str, str] | None = None) -> Path | None:
    """Return the verified bundled Node.js executable, when available."""

    return _private_desktop_runtime_file(DESKTOP_NODE_BIN_ENV, base_env, executable=True)


def private_desktop_npm_cli(base_env: Mapping[str, str] | None = None) -> Path | None:
    """Return the verified bundled npm CLI entrypoint, when available."""

    return _private_desktop_runtime_file(DESKTOP_NPM_CLI_ENV, base_env)


def private_desktop_backends_root(base_env: Mapping[str, str] | None = None) -> Path | None:
    """Return the mutable app-private backend root supplied by the launcher."""

    env = os.environ if base_env is None else base_env
    value = env.get(DESKTOP_BACKENDS_ROOT_ENV, "")
    if not value:
        return None
    root = Path(value).expanduser()
    if not root.is_absolute() or root.is_symlink():
        return None
    resolved = root.resolve(strict=False)
    runtime_root = private_desktop_runtime_root(env)
    if runtime_root is None:
        return None
    try:
        resolved.relative_to(runtime_root)
        return None
    except ValueError:
        pass
    try:
        runtime_root.relative_to(resolved)
        return None
    except ValueError:
        return resolved


def _private_desktop_runtime_file(
    env_name: str,
    base_env: Mapping[str, str] | None,
    *,
    executable: bool = False,
) -> Path | None:
    env = os.environ if base_env is None else base_env
    value = env.get(env_name, "")
    if not value:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    root = private_desktop_runtime_root(env)
    if root is None:
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    if not resolved.is_file() or (executable and not os.access(resolved, os.X_OK)):
        return None
    return resolved


def is_private_desktop_runtime_path(
    path: str | os.PathLike[str] | None,
    base_env: Mapping[str, str] | None = None,
) -> bool:
    """Whether *path* belongs to the verified app-private Runtime tree."""

    if not path:
        return False
    root = private_desktop_runtime_root(base_env)
    if root is None:
        return False
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        return False
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError:
        return False
    return True


def _normalized_bind_host(bind_host: str | None) -> str:
    """Turn a configured setup host into something a socket can actually bind.

    ``ui.setup_host`` holds what a person typed, and two of those spellings are
    not addresses: ``[::1]`` is a URL rendering and ``*`` is a word for "every
    interface". ``getaddrinfo`` rejects both, so any listener built from them
    fails at bind -- which is where ``*`` has always failed, uvicorn's own bind
    on ``master`` included. Resolving them here keeps the knowledge in one place
    instead of teaching each listener the spellings.
    """

    host = (bind_host or "127.0.0.1").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host == "*":
        return "0.0.0.0"
    return host


def _bind_family(bind_host: str | None) -> socket.AddressFamily:
    host = _normalized_bind_host(bind_host)
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def desktop_loopback_host(bind_host: str | None) -> str:
    """Return the literal loopback matching the primary bind's IP family."""

    return "::1" if _bind_family(bind_host) == socket.AF_INET6 else "127.0.0.1"


def requires_desktop_loopback_listener(bind_host: str | None) -> bool:
    """Whether a specific primary bind needs a second loopback listener."""

    host = _normalized_bind_host(bind_host)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Match the first address the same-family socket bind will select. A
        # loopback-only hostname already serves the desktop; an unresolved or
        # non-loopback name still needs the companion listener.
        try:
            resolved = socket.getaddrinfo(
                host,
                None,
                family=_bind_family(host),
                type=socket.SOCK_STREAM,
            )
            address = ipaddress.ip_address(resolved[0][4][0])
        except (IndexError, OSError, ValueError):
            return True
    advertised = ipaddress.ip_address(desktop_loopback_host(host))
    return not address.is_unspecified and address != advertised


def ui_listener_hosts(bind_host: str | None) -> tuple[str, ...]:
    """Return the primary listener plus any desktop-only loopback listener."""

    # The same normalisation every other helper here already applies, so the
    # host that gets bound and the host they reason about cannot disagree.
    primary = _normalized_bind_host(bind_host)
    if requires_desktop_loopback_listener(primary):
        return primary, desktop_loopback_host(primary)
    return (primary,)


def normalize_desktop_port(port: int | str) -> int:
    if isinstance(port, str):
        if not port.isascii() or not port.isdecimal():
            raise ValueError("desktop endpoint port must be between 1 and 65535")
        port = int(port)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("desktop endpoint port must be between 1 and 65535")
    return port


def desktop_origin(bind_host: str | None, port: int | str) -> str:
    """Build the desktop shell's exact loopback origin."""

    normalized_port = normalize_desktop_port(port)
    loopback = desktop_loopback_host(bind_host)
    rendered_host = f"[{loopback}]" if ":" in loopback else loopback
    return f"http://{rendered_host}:{normalized_port}"


def desktop_endpoint_payload(
    bind_host: str | None,
    port: int | str,
) -> DesktopEndpointPayload:
    """Return the frozen schema-v1 descriptor consumed by the desktop shell."""

    return {
        "schema_version": DESKTOP_ENDPOINT_SCHEMA_VERSION,
        "origin": desktop_origin(bind_host, port),
    }
