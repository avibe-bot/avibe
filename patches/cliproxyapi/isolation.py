"""Local-only test envelope, not an engine launcher or release integration."""

from __future__ import annotations

import json
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys


STORAGE_ENV = (
    "HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
    "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "AVIBE_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
)
PRODUCT_DIRS = (".avibe", ".vibe_remote", ".codex", ".claude")
XDG_DEFAULTS = {
    "XDG_CONFIG_HOME": ".config", "XDG_CACHE_HOME": ".cache",
    "XDG_DATA_HOME": ".local/share", "XDG_STATE_HOME": ".local/state",
}
PROOF_PATH = Path("/run/avibe-engine-test-isolation.json")

# A finite bootstrap caller, not an arbitrary Git command runner. Configuration
# that can load code, redirect objects/worktrees or select transports is refused
# in the local repository, not inherited from the operator's account.
GIT_COMMANDS = {"init", "fetch", "checkout", "rev-parse", "archive", "status", "diff", "ls-files", "apply"}
GIT_LOCAL_KEYS = {
    "core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates",
    "core.ignorecase", "core.precomposeunicode", "user.name", "user.email",
}


def safe_git(repository: Path, *arguments: str) -> bytes:
    """Use the installed Git without ambient config, hooks, filters or helpers.

    Original storage is captured by the caller before this command-local
    environment is constructed. Never mutate os.environ or an installed config.
    Local config is listed without includes before any operational command;
    only ordinary repository bookkeeping/remote declarations are admitted.
    """
    if not arguments or arguments[0] not in GIT_COMMANDS:
        raise ValueError("Unsupported recipe Git operation.")
    operation = arguments[0]
    if operation in {"init", "fetch", "checkout", "apply"}:
        repository = validate_state_root(repository)
    environment = {
        "PATH": "/usr/bin:/bin", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_COUNT": "0", "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
    }
    executable = Path("/usr/bin/git")
    info = executable.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError("The installed /usr/bin/git must be trusted.")
    command = [
        str(executable), "--no-pager", "-c", "core.hooksPath=/dev/null",
        "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
        "-c", "core.attributesFile=/dev/null", "-c", "core.excludesFile=/dev/null",
        "-c", "credential.helper=", "-c", "protocol.allow=never",
        "-c", "protocol.https.allow=always",
    ]
    options = {"env": environment, "cwd": "/", "stdin": subprocess.DEVNULL,
               "close_fds": True, "timeout": 30}
    if operation == "init":
        if len(arguments) != 1 or repository.is_symlink() or (
                repository.exists() and any(repository.iterdir())):
            raise ValueError("Git initialization requires a fresh empty destination.")
        return subprocess.check_output([*command, "init", "--template=", str(repository)], **options)
    prefix = [*command, "-C", str(repository)]
    configuration = subprocess.check_output(
        [*prefix, "config", "--local", "--no-includes", "--null", "--list"], **options,
    )
    for entry in configuration.split(b"\0"):
        if not entry:
            continue
        key = entry.split(b"\n", 1)[0].decode("utf-8").lower()
        bookkeeping = ((key.startswith("remote.") and key.endswith((".url", ".fetch")))
                       or (key.startswith("branch.") and key.endswith((".remote", ".merge"))))
        if key not in GIT_LOCAL_KEYS and not bookkeeping:
            # Do not print config values (or potentially protected include paths).
            raise ValueError("Recipe Git refuses non-bookkeeping local configuration.")
    if operation == "diff":
        arguments = ("diff", "--no-ext-diff", "--no-textconv", *arguments[1:])
    elif operation == "fetch":
        arguments = ("fetch", "--no-recurse-submodules", *arguments[1:])
    return subprocess.check_output([*prefix, *arguments], **options)


def _absolute(value: str, name: str) -> Path:
    # Never include configured values in exceptions or diagnostic output.
    if (not isinstance(value, str) or not value or len(value) > 4096
            or "\0" in value or not Path(value).is_absolute() or ".." in Path(value).parts):
        raise ValueError(f"{name} must be an absolute storage path without parent traversal.")
    return Path(value)


def _aliases(paths) -> tuple[Path, ...]:
    try:
        return tuple(sorted({alias for path in paths for alias in (path, path.resolve())}))
    except (OSError, RuntimeError):
        raise ValueError("Configured storage aliases cannot be resolved safely.") from None


@dataclass(frozen=True, repr=False)
class StorageContext:
    """Original user locations, never inferred from a generated task environment."""

    uid: int
    homes: tuple[Path, ...]
    protected: tuple[Path, ...]
    environment_sha256: str

    @classmethod
    def capture(cls, *, uid: int | None = None, environment=None):
        uid = os.getuid() if uid is None else uid
        environment = os.environ if environment is None else environment
        selected = {name: environment.get(name) for name in STORAGE_ENV}
        # Empty/unset values use defaults; runtime has no default. Relative
        # and malformed configured values fail closed, including overridden HOME.
        configured = {name: _absolute(value, name) for name, value in selected.items() if value}
        passwd_home = _absolute(pwd.getpwuid(uid).pw_dir, "passwd home")
        effective_home = configured.get("HOME", passwd_home)
        homes = _aliases((passwd_home, effective_home))
        protected = [home / name for home in homes for name in PRODUCT_DIRS]
        protected += [configured.get(name, effective_home / default) for name, default in XDG_DEFAULTS.items()]
        protected += [configured[name] for name in (
            "XDG_RUNTIME_DIR", "AVIBE_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
        ) if name in configured]
        environment_sha = hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()
        return cls(uid, homes, _aliases(protected), environment_sha)

    def record(self) -> dict:
        """Private parent control/proof only: do not print protected locations."""
        return {"version": 1, "uid": self.uid, "homes": list(map(str, self.homes)),
                "protected": list(map(str, self.protected)),
                "environment_sha256": self.environment_sha256}

    @classmethod
    def from_parent(cls, record: dict):
        if (not isinstance(record, dict) or set(record) != {
                "version", "uid", "homes", "protected", "environment_sha256",
            } or record["version"] != 1 or type(record["uid"]) is not int
                or not isinstance(record["homes"], list) or not record["homes"]
                or not isinstance(record["protected"], list) or not record["protected"]
                or len(record["homes"]) > 16 or len(record["protected"]) > 128
                or not isinstance(record["environment_sha256"], str)
                or len(record["environment_sha256"]) != 64):
            raise RuntimeError("Invalid parent storage context.")
        # These paths were canonicalized outside the chroot. Resolving them
        # again here would lose aliases hidden by the private mount namespace.
        return cls(record["uid"], tuple(_absolute(value, "parent home") for value in record["homes"]),
                   tuple(_absolute(value, "parent storage") for value in record["protected"]),
                   record["environment_sha256"])

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.record(), sort_keys=True).encode()).hexdigest()

    def with_root_identity(self):
        root = self.capture(uid=0, environment={})
        return StorageContext(self.uid, tuple(sorted(set(self.homes + root.homes))),
                              tuple(sorted(set(self.protected + root.protected))), self.environment_sha256)

    def validate(self, root: Path) -> Path:
        lexical = root.absolute()
        canonical = root.resolve()
        broad = {Path(name).resolve() for name in ("/", "/tmp", "/var/tmp", "/var/folders", "/home", "/Users", "/root")}
        for target in (lexical, canonical):
            if target in broad or any(target == home or home.is_relative_to(target) for home in self.homes):
                raise ValueError("The evidence root must be a dedicated, narrow task directory.")
            if any(target == boundary or target.is_relative_to(boundary) or boundary.is_relative_to(target)
                   for boundary in self.protected):
                raise ValueError("Task writes must not target protected user state.")
        return canonical


def storage_context() -> StorageContext:
    """Only actual private namespace proof may replace ambient user identity."""
    if sys.platform == "linux" and PROOF_PATH.exists():
        proof = _namespace_proof()
        return StorageContext.from_parent(proof["storage_context"])
    if os.geteuid() == 0 or "SUDO_UID" in os.environ:
        raise RuntimeError("Original caller storage context is required before sudo sanitization.")
    return StorageContext.capture()


def validate_state_root(root: Path, *, owner_uid: int | None = None, context: StorageContext | None = None) -> Path:
    """Validate every public write root before setup, including canonical aliases."""
    context = storage_context() if context is None else context
    if owner_uid is not None and context.uid != owner_uid:
        raise RuntimeError("Storage context does not belong to the invoking owner.")
    return context.validate(root)


def validate_temporary_root(root: Path, *, owner_uid: int | None = None, context: StorageContext | None = None) -> Path:
    """The privileged recipe admits only narrow canonical temporary children."""
    root = validate_state_root(root, owner_uid=owner_uid, context=context)
    if not any(root != base and root.is_relative_to(base) for base in (Path("/tmp"), Path("/var/tmp"))):
        raise ValueError("Privileged scratch must be a canonical child of /tmp or /var/tmp.")
    return root


def _namespace_proof() -> dict:
    """Private proof reader; no ambient marker or caller dictionary grants trust."""
    if sys.platform != "linux":
        raise RuntimeError("Network suites require namespace.py's private Linux envelope.")
    fd = os.open(PROOF_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0
                or info.st_mode & 0o022):
            raise RuntimeError("Network suites require namespace.py's private Linux envelope.")
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise RuntimeError("Oversized namespace proof.")
    proof = json.loads(raw)
    required = {"mnt", "net", "pid"}
    if set(proof["namespaces"]) != required or set(proof["outer_namespaces"]) != required:
        raise RuntimeError("All three namespace identities are required.")
    for name, expected in proof["namespaces"].items():
        if os.readlink(f"/proc/self/ns/{name}") != expected or expected == proof["outer_namespaces"][name]:
            raise RuntimeError("Namespace identity does not match the isolated envelope.")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    if any(int(status[name].strip(), 16) for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")):
        raise RuntimeError("Candidate process still has Linux capabilities.")
    if (status["NoNewPrivs"].strip() != "1" or os.getuid() == 0
            or os.getuid() != os.geteuid() or os.getuid() != proof["uid"] or os.getgid() != proof["gid"]):
        raise RuntimeError("Candidate process has not dropped privilege.")
    context = StorageContext.from_parent(proof["storage_context"])
    if context.uid != proof["uid"] or context.fingerprint() != proof["storage_sha256"]:
        raise RuntimeError("Parent storage context identity does not match.")
    root = _absolute(proof["root"], "parent task root")
    output = _absolute(proof["output"], "parent output")
    state = _absolute(proof["state"], "parent cache")
    receipt = proof["receipt"]
    if (not isinstance(receipt, str) or Path(receipt).name != receipt or receipt in ("", ".", "..")
            or output != root / "runs" / receipt or state == root or not state.is_relative_to(root)
            or state == output or state.is_relative_to(output) or output.is_relative_to(state)
            or output.is_symlink() or output.stat().st_uid != proof["uid"]
            or stat.S_IMODE(output.stat().st_mode) != 0o750):
        raise RuntimeError("Private task ownership does not match the exclusive invocation.")
    for target in (root, output, state):
        if context.validate(target) != target:
            raise RuntimeError("Private task roots must match parent canonical identities.")
    proof["process_status"] = {
        name: status[name].strip()
        for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "NoNewPrivs")
    }
    proof["actual_uid"], proof["actual_gid"] = os.getuid(), os.getgid()
    return proof


def namespace_receipt() -> dict:
    """Public evidence excludes original user paths; retain only their digest."""
    proof = _namespace_proof()
    return {name: value for name, value in proof.items() if name != "storage_context"}


def environment_directories(root: Path, cache: Path) -> list[tuple[Path, str]]:
    directories = [(root, name) for name in ("home", "tmp", "config", "data")]
    return directories + [(cache, name) for name in ("cache", "go", "mod")]


def preparation_directories(root: Path) -> tuple[Path, ...]:
    """Admit the entire documented setup plan, without creating anything."""
    context = storage_context()
    canonical = validate_temporary_root(root, context=context)
    if root != canonical:
        raise ValueError("Preparation requires an absolute canonical task directory.")
    state = root / "state"
    directories = [root / name for name in (
        "recipe", "source", "state", "downloads", "toolchain", "uv-toolchain", "venv", "fixtures",
    )]
    directories += [owner / name for owner, name in environment_directories(state, state)]
    directories.append(state / "uv-cache")
    # Check actual descendants and their aliases before even the first mkdir,
    # copy, Git invocation or export. An admitted ancestor is not sufficient.
    for path in directories:
        if context.validate(path) != path:
            raise ValueError("Preparation destinations must not use aliases.")
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != context.uid
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("Preparation requires a caller-owned mode-0700 task directory.")
    if any(root.iterdir()):
        raise ValueError("Preparation requires an empty task; preserve all existing contents.")
    return tuple(directories)


def isolated_environment(root: Path, *, cache: Path | None = None, go: Path | None = None) -> dict[str, str]:
    context = storage_context()
    root = context.validate(root)
    cache = context.validate(cache) if cache is not None else root
    directories = environment_directories(root, cache)
    # Validate all aliases before the FIRST write, not while creating folders.
    for owner, name in directories:
        target = context.validate(owner / name)
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
    context = storage_context()
    root = context.validate(root)
    if sys.platform == "linux":
        proof = namespace_receipt()
        if Path(proof["state"]) != root.resolve():
            raise RuntimeError("State does not belong to this isolated envelope.")
        return []
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise RuntimeError("This recipe requires macOS sandbox-exec; no unsandboxed fallback.")
    def quote(value: Path) -> str:
        return json.dumps(str(value))
    policy = (
        '(version 1) (allow default) (deny network-outbound) '
        '(deny file-write*) '
        f'(allow file-write* (subpath {quote(root)})) '
        '(allow file-write* (subpath "/dev")) '
    )
    for boundary in context.protected:
        policy += f'(deny file-read* (subpath {quote(boundary)})) '
    return ["/usr/bin/sandbox-exec", "-p", policy]
