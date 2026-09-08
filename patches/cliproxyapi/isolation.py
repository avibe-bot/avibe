"""Local-only test envelope, not an engine launcher or release integration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import shutil
import sys


def isolated_environment(root: Path) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("home", "tmp", "config", "cache", "data", "go", "mod"):
        (root / name).mkdir(exist_ok=True)
    return {
        "PATH": os.defpath + os.pathsep + str(Path(shutil.which("go") or "/usr/bin/go").parent),
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "AVIBE_HOME": str(root / "home" / ".avibe"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_DATA_HOME": str(root / "data"),
        "GOPATH": str(root / "go"),
        "GOMODCACHE": str(root / "mod"),
        "GOCACHE": str(root / "cache"),
        "GOENV": "off",
        "GOTOOLCHAIN": "go1.26.4",
        "GOMAXPROCS": "2",
        "GOMEMLIMIT": "1536MiB",
        "CGO_ENABLED": "0",
        "GOPROXY": "off",
        "GOSUMDB": "sum.golang.org",
        "HTTP_PROXY": "http://127.0.0.1:1",
        "HTTPS_PROXY": "http://127.0.0.1:1",
        "NO_PROXY": "127.0.0.1,localhost",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def sandbox_prefix(root: Path) -> list[str]:
    """Fail closed outside the verified macOS loopback-only envelope."""
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise RuntimeError("This recipe requires macOS sandbox-exec; no unsandboxed fallback.")
    root = root.resolve(strict=True)
    user_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if root == Path("/") or root == user_home or user_home.is_relative_to(root):
        raise ValueError("The evidence root must be a dedicated, narrow task directory.")
    def quote(value: Path) -> str:
        return json.dumps(str(value))
    policy = (
        '(version 1) (allow default) (deny network-outbound) '
        '(allow network-outbound (remote ip "localhost:*")) '
        '(deny file-write*) '
        f'(allow file-write* (subpath {quote(root)})) '
        '(allow file-write* (subpath "/dev")) '
    )
    for name in (".avibe", ".vibe_remote", ".codex", ".claude"):
        policy += f'(deny file-read* (subpath {quote(user_home / name)})) '
    return ["/usr/bin/sandbox-exec", "-p", policy]
