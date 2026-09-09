"""Local-only test envelope, not an engine launcher or release integration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import shutil
import sys


def validate_state_root(root: Path, *, owner_uid: int | None = None) -> Path:
    """Validate every public write root before setup, including canonical aliases."""
    root = root.resolve()
    broad = {Path(name).resolve() for name in ("/", "/tmp", "/var/tmp", "/var/folders", "/home", "/Users", "/root")}
    if root in broad:
        raise ValueError("The evidence root must be a dedicated, narrow task directory.")
    for uid in {os.getuid(), owner_uid if owner_uid is not None else os.getuid()}:
        user_home = Path(pwd.getpwuid(uid).pw_dir).resolve()
        if root == user_home or user_home.is_relative_to(root):
            raise ValueError("The evidence root must be a dedicated, narrow task directory.")
        for name in (".avibe", ".vibe_remote", ".codex", ".claude"):
            protected = user_home / name
            # Include the invoking owner when this is the privileged parent.
            # Check lexical protection as well as compatibility symlinks.
            for boundary in (protected, protected.resolve()):
                if root == boundary or root.is_relative_to(boundary) or boundary.is_relative_to(root):
                    raise ValueError("Task writes must not target protected user state.")
    return root


def validate_temporary_root(root: Path, *, owner_uid: int | None = None) -> Path:
    """The privileged recipe admits only narrow canonical temporary children."""
    root = validate_state_root(root, owner_uid=owner_uid)
    if not any(root != base and root.is_relative_to(base) for base in (Path("/tmp"), Path("/var/tmp"))):
        raise ValueError("Privileged scratch must be a canonical child of /tmp or /var/tmp.")
    return root


def namespace_receipt() -> dict:
    """Fail closed unless the root-created private envelope matches this process."""
    path = Path("/run/avibe-engine-test-isolation.json")
    if sys.platform != "linux" or not path.is_file() or path.stat().st_uid != 0:
        raise RuntimeError("Network suites require namespace.py's private Linux envelope.")
    proof = json.loads(path.read_text())
    required = {"mnt", "net", "pid"}
    if set(proof["namespaces"]) != required or set(proof["outer_namespaces"]) != required:
        raise RuntimeError("All three namespace identities are required.")
    for name, expected in proof["namespaces"].items():
        if os.readlink(f"/proc/self/ns/{name}") != expected or expected == proof["outer_namespaces"][name]:
            raise RuntimeError("Namespace identity does not match the isolated envelope.")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    if any(int(status[name].strip(), 16) for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")):
        raise RuntimeError("Candidate process still has Linux capabilities.")
    if status["NoNewPrivs"].strip() != "1" or os.getuid() == 0:
        raise RuntimeError("Candidate process has not dropped privilege.")
    proof["process_status"] = {
        name: status[name].strip()
        for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs")
    }
    proof["actual_uid"], proof["actual_gid"] = os.getuid(), os.getgid()
    return proof


def isolated_environment(root: Path, *, cache: Path | None = None, go: Path | None = None) -> dict[str, str]:
    root = validate_state_root(root)
    cache = validate_state_root(cache) if cache is not None else root
    directories = [(root, name) for name in ("home", "tmp", "config", "data")]
    directories += [(cache, name) for name in ("cache", "go", "mod")]
    # Validate all aliases before the FIRST write, not while creating folders.
    for owner, name in directories:
        target = validate_state_root(owner / name)
        if not target.is_relative_to(owner):
            raise ValueError("Environment directory escapes its task root.")
    root.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    for owner, name in directories:
        (owner / name).mkdir(exist_ok=True)
    return {
        "PATH": str((go or Path(shutil.which("go") or "/usr/bin/go")).parent) + os.pathsep + os.defpath,
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "AVIBE_HOME": str(root / "home" / ".avibe"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(cache / "cache"),
        "XDG_DATA_HOME": str(root / "data"),
        "GOPATH": str(cache / "go"),
        "GOMODCACHE": str(cache / "mod"),
        "GOCACHE": str(cache / "cache"),
        "GOENV": "off",
        "GOTOOLCHAIN": "local",
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
    """Deny all network egress for macOS pure-source diagnostics."""
    root = validate_state_root(root)
    if sys.platform == "linux":
        proof = namespace_receipt()
        if Path(proof["state"]) != root.resolve():
            raise RuntimeError("State does not belong to this isolated envelope.")
        return []
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise RuntimeError("This recipe requires macOS sandbox-exec; no unsandboxed fallback.")
    user_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    def quote(value: Path) -> str:
        return json.dumps(str(value))
    policy = (
        '(version 1) (allow default) (deny network-outbound) '
        '(deny file-write*) '
        f'(allow file-write* (subpath {quote(root)})) '
        '(allow file-write* (subpath "/dev")) '
    )
    for name in (".avibe", ".vibe_remote", ".codex", ".claude"):
        policy += f'(deny file-read* (subpath {quote(user_home / name)})) '
    return ["/usr/bin/sandbox-exec", "-p", policy]
