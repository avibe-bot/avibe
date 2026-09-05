#!/usr/bin/env python3
"""Manage Incus-backed Avibe regression environments.

The runner uses Incus as a long-lived system environment, not as a Docker-like
image rebuild wrapper. Slow-moving dependencies live in a reusable base image;
Avibe source is synced into the instance and the service is restarted.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import textwrap
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Container, Iterable, Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.show_runtime_source import retired_show_runtime_source

try:
    import fcntl
except ImportError:  # pragma: no cover - Incus runner is not used on Windows.
    fcntl = None


PROJECT_PREFIX = "avr-"
WORKTREE_PROJECT_PREFIX = "avr-wt-"
INSTANCE_PREFIX = "avibe-"
WORKTREE_INSTANCE_PREFIX = "avibe-wt-"
MASTER_TARGET = "master"
WORKTREE_TARGET = "worktree"
TARGETS = {MASTER_TARGET, WORKTREE_TARGET}
SERVICE_USER = "avibe"
SERVICE_HOME = f"/home/{SERVICE_USER}"
AVIBE_HOME = f"{SERVICE_HOME}/.avibe"
LEGACY_HOME = f"{SERVICE_HOME}/.vibe_remote"
SOURCE_DIR = "/opt/avibe/source"
VENV_DIR = "/opt/avibe/venv"
METADATA_DIR = "/var/lib/avibe-regression"
METADATA_PATH = f"{METADATA_DIR}/metadata.json"
FINGERPRINT_PATH = f"{METADATA_DIR}/fingerprints.json"
SERVICE_NAME = "avibe-regression.service"
# Directories under ``ui/`` that a build produces or installs, as opposed to
# reads. They are excluded from the UI source fingerprint and are the ones a
# sync keeps in place so an unchanged front end never pays for ``npm ci``.
UI_NON_SOURCE_DIRS = ("node_modules", "dist", ".vite")
INTERNAL_DISPATCH_SOCKET = "/tmp/vibe_remote/dispatch.sock"
DEFAULT_IMAGE = "avibe-regression-base-current"
DEFAULT_BASE_SOURCE_IMAGE = "images:ubuntu/24.04/cloud"
DEFAULT_NETWORK = "incusbr0"
DEFAULT_STORAGE_POOL = "default"
DEFAULT_UI_PORT = 5123
CONTAINER_UI_HOST = "127.0.0.1"
DEFAULT_MASTER_HOST_PORT = 15130
DEFAULT_WORKTREE_PORT_START = 15200
DEFAULT_WORKTREE_PORT_END = 15399
ENV_FILE_NAME = ".env.regression"
ENV_PREFIX = "REGRESSION_"
SHOW_RUNTIME_BUILD_TIMEOUT_SECONDS = 300
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")
DAEMON_UNREACHABLE_HINT = (
    "The daemon did not answer, so whether the environment already exists is unknown and the "
    "runner will not guess. On macOS the daemon lives in the Lima VM, so plain `incus` cannot "
    "reach it: set INCUS_CMD, e.g. INCUS_CMD='limactl shell avibe-incus-regression -- sudo incus'. "
    "A daemon that is up but stalled (\"context deadline exceeded\", \"no available cowsql leader "
    "server found\") usually means the VM is starved for IO or memory; let it recover, then retry."
)


class RegressionError(RuntimeError):
    """A user-correctable regression runner error."""


@dataclass(frozen=True)
class RegressionTarget:
    target: str
    slug: str
    project: str
    instance: str
    host_port: int
    ui_host: str
    ui_port: int


class Runner:
    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    def run(
        self,
        command: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        input_text: str | None = None,
        check: bool = True,
        capture: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        print("+ " + shlex.join(command))
        if self.dry_run:
            return subprocess.CompletedProcess(list(command), 0, "", "")
        kwargs: dict = {
            "check": check,
            "text": input_bytes is None,
            "capture_output": capture,
        }
        if input_bytes is not None:
            kwargs["input"] = input_bytes
        elif input_text is not None:
            kwargs["input"] = input_text
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            return subprocess.run(list(command), **kwargs)
        except subprocess.TimeoutExpired as exc:
            detail = f"Command timed out after {timeout:g} seconds: {shlex.join(command)}"
            print(detail, file=sys.stderr)
            raise RegressionError(detail) from exc

    def records(self, command: Sequence[str], *, what: str) -> list[dict]:
        """Return the objects the daemon enumerated, or raise if it could not answer.

        Existence must come from a listing the daemon actually completed, never
        from a lookup's exit status: `incus` exits non-zero both when an object
        is genuinely absent and when the daemon is unreachable or its database
        is stalled. Collapsing the second into the first turns "cannot tell"
        into "not there", and callers then set out to create what already
        exists.

        A zero exit is not on its own an answer either. An entry this runner
        cannot read -- a client and daemon disagreeing about the listing schema,
        say -- used to be filtered out, which turns a listing nobody could parse
        into an inventory that looks complete and happens to be empty. That reads
        as a confirmed absence, and `reconcile --yes` acts on it by releasing
        host ports that are still in use. Every entry is therefore either
        understood or fatal.
        """
        if self.dry_run:
            return []
        result = subprocess.run(list(command), capture_output=True, text=True)
        if result.returncode != 0:
            raise RegressionError(f"Could not list {what}: {daemon_failure_detail(result)}\n{DAEMON_UNREACHABLE_HINT}")
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RegressionError(f"Could not parse the {what} listing returned by Incus: {exc}") from exc
        if not isinstance(payload, list):
            raise RegressionError(f"Unexpected {what} listing returned by Incus: {type(payload).__name__}")
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise RegressionError(
                    f"Unreadable entry in the {what} listing returned by Incus: {item!r}\n"
                    "Every caller identifies an object by name, so an entry without one cannot be reasoned about."
                )
        return payload

    def names(self, command: Sequence[str], *, what: str) -> list[str]:
        """The names from `records`, for callers that only ask whether something exists."""
        return [item["name"] for item in self.records(command, what=what)]


def incus(*args: str, project: str | None = None) -> list[str]:
    command = shlex.split(os.environ.get("INCUS_CMD", "incus"))
    if project:
        command.extend(["--project", project])
    command.extend(args)
    return command


def normalized_remote(value: str) -> str | None:
    """One spelling of "the local daemon" past argv.

    `remote_ref` has always read an empty name as this machine's daemon, and the
    metadata accessor reads `--remote` itself, so an unexpanded
    `--remote "$INCUS_REMOTE"` was two authorities at once: the environment was
    created locally while the accessor bound to some other daemon and recorded
    nothing, leaving the host port allocated with no row naming it. Either reader
    could be written to agree with the other, which is why agreement is not the
    fix -- normalizing at the parser leaves one value to read, for every reader
    that exists now and every one added later.

    That one value has to be a name and not an arbitrary string, because two of
    those readers spell it into places that only hold one: `remote_ref` joins it
    to an object with `:`, and `target_lock_path` makes it part of a filename. A
    separator in it would name a different daemon than the one written, or put a
    lock outside the directory that holds them, so it is refused here -- the one
    point both readers are downstream of.
    """
    name = value.strip()
    if not name:
        return None
    if name in {".", ".."} or any(char in name for char in "/\\:"):
        raise argparse.ArgumentTypeError(
            f"invalid Incus remote name {value!r}: a remote is one name, without '/', '\\' or ':'"
        )
    return name


def remote_ref(remote: str | None, name: str = "") -> str:
    if not remote:
        return name
    return f"{remote}:{name}"


def optional_remote_ref(remote: str | None) -> list[str]:
    return [remote_ref(remote)] if remote else []


def daemon_failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    output = (result.stderr or result.stdout or "").strip().splitlines()
    return output[0] if output else f"exit status {result.returncode}"


def project_exists(runner: Runner, remote: str | None, project: str) -> bool:
    command = incus("project", "list", *optional_remote_ref(remote), "--format", "json")
    return project in runner.names(command, what="Incus projects")


def instance_exists(runner: Runner, remote: str | None, project: str, instance: str) -> bool:
    # An instance cannot outlive its project, so an absent project answers the
    # question without asking Incus to list inside a project it does not have.
    if not project_exists(runner, remote, project):
        return False
    command = incus("list", *optional_remote_ref(remote), "--format", "json", project=project)
    return instance in runner.names(command, what=f"instances in project {project}")


def require_incus() -> None:
    command = shlex.split(os.environ.get("INCUS_CMD", "incus"))
    executable = command[0] if command else "incus"
    if shutil.which(executable) is None:
        raise RegressionError(f"The Incus CLI executable was not found: {executable}")


def validate_slug(slug: str) -> None:
    if not SLUG_RE.match(slug):
        raise RegressionError("Slug must be 3-40 chars, lowercase, and contain only letters, numbers, and hyphens.")


def regression_env(suffix: str, default: str = "") -> str:
    value = os.environ.get(f"{ENV_PREFIX}{suffix}")
    if value is None:
        value = default
    return value.strip()


def regression_show_runtime_source(source: str | None = None) -> str:
    source = regression_env("SHOW_RUNTIME_SOURCE", "archive") if source is None else source.strip()
    source = source or "archive"
    return "archive" if retired_show_runtime_source(source) is not None else source


def regression_show_runtime_env(
    source: str | None,
    service_home: str,
) -> dict[str, str]:
    resolved = regression_show_runtime_source(source)
    env = {"VIBE_SHOW_RUNTIME_SOURCE": resolved}
    if resolved == "archive":
        env["VIBE_SHOW_RUNTIME_ARCHIVE_PATH"] = (
            f"{service_home}/.cache/avibe-regression/vibe-show-runtime-node.tgz"
        )
    return env


def host_bind_env(default: str = "127.0.0.1") -> str:
    return (
        regression_env("PORT_BIND_HOST")
        or os.environ.get("REGRESSION_UI_HOST", "").strip()
        or default
    )


def env_int(name: str) -> int | None:
    if name.startswith(ENV_PREFIX):
        value = regression_env(name[len(ENV_PREFIX):])
    else:
        value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RegressionError(f"{name} must be an integer.") from exc


def current_repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return Path.cwd().resolve()
    return Path(result.stdout.strip()).resolve()


def git_common_root(repo_root: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return repo_root
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = repo_root / common
    return common.resolve().parent


def runtime_root(repo_root: Path) -> Path:
    return git_common_root(repo_root) / ".runtime" / "incus-regression"


def target_lock_path(repo_root: Path, remote: str | None, project: str) -> Path:
    """Where the lock for one environment lives, on this machine.

    An environment is identified by the daemon that holds it and the project on
    it, never by the project alone -- the same rule `worktrees.json` follows,
    because project names are per-daemon and every remote has the same ones. The
    lock file is local wherever the environment is, so a `--remote` run that keyed
    on the project would take the lock of the local environment with that name and
    be read as one, which is what `remote_ref` exists to stop everywhere else.
    Local runs keep exactly the path they have always had: `remote_ref` renders no
    authority as no prefix, so this is byte-identical to every released version,
    and their lock is the same file as ours -- the reason a lock can answer for
    them at all.
    """
    return runtime_root(repo_root) / "locks" / f"{remote_ref(remote, project)}.lock"


@contextmanager
def target_update_lock(repo_root: Path, remote: str | None, project: str, *, dry_run: bool, blocking: bool = True):
    """Serialize runs against one environment, and say so to `reconcile`.

    Keyed on the daemon and the project name rather than on a resolved target,
    because that is the whole key: a caller can name the lock before it has asked
    the mapping for a port, which is what lets the port be allocated inside the
    lock that protects it.

    Which commands have to hold it is a property of the environment rather than
    of any one of them, so it is written here instead of at each call site. Every
    command that changes what a slug names -- its Incus objects, its row, or both
    -- holds it: `up` from before it reserves until after it stamps the row,
    `delete` across removing the objects and forgetting the row. `reconcile` is
    the one command that drops rows without holding it, and deliberately: it
    exists to observe whoever else holds it, so taking it would answer its own
    question. Nothing else belongs to the class. `down`, `status`, `logs` and
    `shell` cannot break what the lock protects -- that a row reserves a host
    port exactly while its environment exists or is being built -- because none
    of them creates, destroys or forgets either half; a `down` that interrupts a
    build makes that `up` fail, and its reservation gives the row back.

    `blocking=False` is for a caller whose answer to "somebody else holds this"
    is to stop rather than to wait. The acquire is then both the proof that
    nobody holds it and the protection, which is the only order with no window
    between the two -- the same reason `target_run_in_flight` trusts a lock it
    took itself and nothing else. Waiting would also be a new reading of a held
    lock: every other reader treats one as a live run whose slug it must leave
    alone, and behind an `up` that is stuck it would never return.
    """
    if dry_run or fcntl is None:
        yield
        return
    lock_path = target_lock_path(repo_root, remote, project)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as fh:
        print(f"Acquiring regression update lock: {lock_path}")
        if blocking:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        else:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Only contention is turned into this message. Any other way the
                # lock can fail is re-raised as itself, because a wrong
                # explanation of a real fault costs more than no explanation.
                raise RegressionError(
                    f"Another run holds the regression update lock for {remote_ref(remote, project)}: {lock_path}\n"
                    "It is building or removing that environment right now. Wait for it to finish, or stop it, then retry."
                ) from None
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def target_run_in_flight(repo_root: Path, remote: str | None, project: str) -> bool:
    """Whether some live process is changing `project` on `remote` right now.

    Changing, not building: a `delete` holds the same lock while it removes the
    environment and its row, and a row kept for the moment that takes is a row
    its holder is about to drop itself. Either way the answer is the same one,
    which is why the question is about the lock and not about what its holder
    intends.

    Asked of the kernel rather than of `worktrees.json`, because no field a run
    writes can answer it. A record lies in both directions: a run that dies
    without unwinding leaves its own behind, and a run from a checkout that
    predates the field writes nothing recognisable at all. The lock above cannot
    do either. The kernel drops it when the holder exits however it exits, so it
    cannot outlive its run, and every version of this runner that has ever built
    a worktree environment takes it, at a path derived from the shared git common
    root -- so this answers for an `up` started from another worktree, an older
    checkout, or an installed release exactly as well as for one of ours. Those
    older runs take the lock a moment after writing their row rather than before
    it, so what is exposed there is that instant, not the build; reading the row
    instead would mean trusting a stamp from another clock, which says nothing
    about whether a run is live. Nor do they carry which daemon they are updating:
    they key the lock on the project alone, so a released `up --remote` takes the
    local environment's lock. That is the one direction still left, and it errs
    towards in flight, which keeps a row rather than dropping a live one.

    Not-yet-known answers in flight, as every unanswered question in `reconcile`
    does: a platform without `flock`, a lock file this user cannot open. Only a
    lock this call took itself proves nobody holds it.
    """
    if fcntl is None:
        return True
    lock_path = target_lock_path(repo_root, remote, project)
    try:
        # Read-only, and no `mkdir`: a probe must not create the artifact whose
        # absence is the answer. `flock` is owned by the open file description,
        # so this conflicts with a lock held by this same process too -- the
        # conservative side, and the only side it could safely land on.
        fd = os.open(lock_path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        # Closing the descriptor drops whatever this probe just took, so the
        # answer costs nothing beyond the moment it was read.
        os.close(fd)
    return False


_held_mapping_locks: set[Path] = set()


@contextmanager
def worktree_mapping_lock(repo_root: Path, *, dry_run: bool):
    """Serialize every read-modify-write of `worktrees.json`, and nest safely.

    Re-entrant on purpose, because the mapping's writers now take this lock
    themselves rather than trusting a caller to have taken it: `up` holds it
    across resolving a target and reserving its slug, `reconcile` holds it across
    a listing and the decision that listing feeds, and both end in a write that
    acquires it again. `flock` is owned by an open file description, not by a
    process, so a second `open` plus `flock` here would block forever on a lock
    this very process holds -- a deadlock rather than a wait. Remembering what is
    already held makes the inner acquisition a no-op and keeps the outer span
    exactly as wide as it was.
    """
    if dry_run or fcntl is None:
        yield
        return
    lock_dir = runtime_root(repo_root) / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = (lock_dir / "worktrees.lock").resolve()
    if lock_path in _held_mapping_locks:
        yield
        return
    with lock_path.open("w", encoding="utf-8") as fh:
        print(f"Acquiring regression worktree mapping lock: {lock_path}")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        _held_mapping_locks.add(lock_path)
        try:
            yield
        finally:
            _held_mapping_locks.discard(lock_path)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def load_env_file(repo_root: Path, env_file: Path | None) -> Path | None:
    common_root = git_common_root(repo_root)
    candidates = [env_file] if env_file else [
        repo_root / ENV_FILE_NAME,
        common_root / ENV_FILE_NAME,
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = candidate.resolve()
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line.removeprefix("export ").strip()
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'\"")
            os.environ.setdefault(key, value)
        return path
    return None


def branch_name(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def commit_sha(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def is_dirty(repo_root: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(result.stdout.strip())


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    if not slug or not slug[0].isalpha():
        slug = f"wt-{slug}" if slug else "wt"
    return slug[:40].strip("-")


def worktree_slug(repo_root: Path, explicit: str | None = None) -> str:
    if explicit:
        slug = slugify(explicit)
    else:
        source = branch_name(repo_root) or repo_root.name
        digest = hashlib.sha1(str(repo_root).encode("utf-8")).hexdigest()[:8]
        slug = f"{slugify(source)[:24]}-{digest}"
    validate_slug(slug)
    return slug


def project_name_for(target: str, slug: str) -> str:
    if target == MASTER_TARGET:
        return f"{PROJECT_PREFIX}master"
    validate_slug(slug)
    return f"{WORKTREE_PROJECT_PREFIX}{slug}"


def instance_name_for(target: str, slug: str) -> str:
    if target == MASTER_TARGET:
        return f"{INSTANCE_PREFIX}master"
    validate_slug(slug)
    return f"{WORKTREE_INSTANCE_PREFIX}{slug}"


def unbracket_host(host: str) -> str:
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def is_ipv6_host(host: str) -> bool:
    try:
        return ":" in unbracket_host(host)
    except ValueError:
        return False


def tcp_endpoint(host: str, port: int) -> str:
    return f"tcp:[{unbracket_host(host)}]:{port}" if is_ipv6_host(host) else f"tcp:{host}:{port}"


def ensure_host_port_available(host: str, port: int) -> None:
    family = socket.AF_INET6 if is_ipv6_host(host) else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((unbracket_host(host), port))
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM}:
                print(f"Warning: cannot preflight privileged host port {host}:{port}; continuing.", file=sys.stderr)
                return
            raise RegressionError(f"Host port {host}:{port} is not available: {exc}") from exc


def mapping_path(repo_root: Path) -> Path:
    return runtime_root(repo_root) / "worktrees.json"


def _load_worktree_mapping(repo_root: Path) -> dict:
    """Read the mapping file. Private: reach it through `WorktreeMetadata`."""
    path = mapping_path(repo_root)
    if not path.is_file():
        return {"schema_version": 1, "worktrees": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"schema_version": 1, "worktrees": {}}
    if not isinstance(payload, dict):
        return {"schema_version": 1, "worktrees": {}}
    payload.setdefault("schema_version", 1)
    payload.setdefault("worktrees", {})
    return payload


def _write_worktree_mapping(repo_root: Path, payload: dict) -> None:
    """Write the mapping file. Private: reach it through `WorktreeMetadata`."""
    path = mapping_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class WorktreeMetadata:
    """`worktrees.json`, bound to the Incus daemon it is evidence about.

    The file reserves host ports on this machine and records what this machine's
    daemon holds, so every read of it and every write to it is a claim about
    exactly one authority. Binding that authority to the accessor is what makes
    the claim true by construction: a caller holding one of these cannot name
    another daemon's metadata, so it cannot read or write it.

    A predicate each command was expected to consult came first, and being a
    question is what made it forgettable. `delete --remote` and
    `reconcile --remote` asked it; `up --remote` did not, and so a remote
    environment reserved a host port on this machine, overwrote whatever live
    local row shared its slug, and -- because the other two commands had learned
    to keep the file -- left that reservation behind for good. The reads never
    asked at all: a remote `up` took its port from a local row, and a remote
    `reconcile` printed local provenance beside remote environments and called
    local-only rows environments the remote had lost.
    """

    repo_root: Path
    remote: str | None = None

    @property
    def owned(self) -> bool:
        """Whether the daemon in question is the one this file describes."""
        return self.remote is None

    @contextmanager
    def locked(self, *, dry_run: bool):
        """Hold the mapping lock across a decision that reads and then writes.

        Taken here rather than by each command, because a lock on this file is a
        claim about one authority too, and the commands had learned that about
        the rows without learning it about the lock. `reconcile --remote` held it
        across two listings of another daemon, where this accessor exposes no
        rows and writes none: a slow or unreachable remote blocked every local
        `up` from reserving a port for as long as the listing took, protecting
        nothing, since nothing of this file's was in the span.
        """
        if not self.owned:
            yield
            return
        with worktree_mapping_lock(self.repo_root, dry_run=dry_run):
            yield

    def rows(self) -> dict:
        """The recorded rows, or none at all when another daemon is the subject."""
        if not self.owned:
            return {}
        return _load_worktree_mapping(self.repo_root).get("worktrees") or {}

    def mutate(self, mutate: Callable[[dict], None]) -> None:
        """The only writer of `worktrees.json`: lock, load, apply, save.

        Owning the sequence here is what makes "the mapping is only ever
        modified under the mapping lock" true by construction instead of true
        whenever every caller remembers. One caller did not: `up` stamps
        completion after the command has already released the lock, so that
        load-modify-save could interleave with reconcile's, and whichever saved
        last erased the other -- restoring a row that was just pruned, or
        dropping the stamp that proves a reservation finished and leaving the
        slug pending forever.

        `mutate` receives the `worktrees` mapping itself, loaded inside the
        lock, so it cannot act on a copy read before the lock was held.
        """
        if not self.owned:
            return
        with self.locked(dry_run=False):
            payload = _load_worktree_mapping(self.repo_root)
            mutate(payload.setdefault("worktrees", {}))
            _write_worktree_mapping(self.repo_root, payload)

    def allocated_ports(self) -> set[int]:
        return {
            item["host_port"]
            for item in self.rows().values()
            if isinstance(item, dict) and isinstance(item.get("host_port"), int)
        }

    def port_for(self, slug: str) -> int | None:
        item = self.rows().get(slug)
        if isinstance(item, dict) and isinstance(item.get("host_port"), int):
            return item["host_port"]
        return None

    def reserve(self, target: RegressionTarget, *, dry_run: bool = False) -> WorktreeReservation:
        """Record the slug and its port before the environment is built.

        The row carries an opaque claim minted here and the reservation handed
        back carries the same value, so ending the reservation later can compare
        instead of remember. It has to: `reserve` merges over whatever row it
        finds, which is exactly how a second `up` on this slug takes a row this
        run wrote, and the claim is what makes that takeover observable
        afterwards. `up` now reserves under the slug's update lock, so a takeover
        while the first run is still building takes a platform without `flock`;
        the claim is what keeps the property from depending on that.

        A claim is minted only when this run actually wrote a row, which is the
        one condition the reservation then needs: no claim covers a dry run, a
        target that owns no row, and a remote accessor that writes nothing --
        each of which used to be re-derived at every end of the reservation.

        The row binds the slug to a port and a pair of object names; it says
        nothing about what is built there, because this run has not built it yet,
        and nothing about whether the run is still alive, because no field can
        (`target_run_in_flight` is where that is asked). `complete` owns the
        branch and the commit for the first reason: the merge means anything
        written here lands beside the previous run's fields, and a reservation
        that wrote its own branch produced a row reporting the new branch for an
        environment still built from the old one's commit.
        """
        reservation = WorktreeReservation(metadata=self, target=target, dry_run=dry_run)
        if dry_run or not self.owned or target.target != WORKTREE_TARGET:
            return reservation
        reservation.claim = os.urandom(8).hex()
        row = {
            "path": str(self.repo_root),
            "project": target.project,
            "instance": target.instance,
            "host_port": target.host_port,
            "reserved_at": datetime.now(timezone.utc).isoformat(),
            "claim": reservation.claim,
        }
        self.mutate(lambda worktrees: worktrees.setdefault(target.slug, {}).update(row))
        return reservation

    def apply_claimed(self, target: RegressionTarget, claim: str, row: dict | None) -> None:
        """Replace or drop a slug's row while it is still the one `claim` wrote.

        Both ends of a reservation write through here, because "this row is still
        mine" is one property, and a second implementation of it is how the first
        one gets forgotten. It was: the comparison was added to the release while
        completion kept writing unconditionally, so an `up` that finished first
        replaced a newer run's row -- and with it that run's port and its claim,
        leaving the port allocated to a slug whose row no longer records it while
        the newer `up` was still building against it.

        The comparison happens inside the write's own load-modify-save, under the
        lock, because it is a claim about the row as it is now: between reserving
        and writing, another `up` on this slug may have merged its own row over
        this one. Read the row first and this becomes the accident it exists to
        prevent.
        """

        def guarded(worktrees: dict) -> None:
            current = worktrees.get(target.slug)
            if not isinstance(current, dict) or current.get("claim") != claim:
                return
            if row is None:
                del worktrees[target.slug]
            else:
                worktrees[target.slug] = row

        self.mutate(guarded)

    def complete(self, target: RegressionTarget, claim: str) -> None:
        """Stamp the environment as built, replacing the row, its claim and its `reserved_at`."""
        self.apply_claimed(
            target,
            claim,
            {
                "path": str(self.repo_root),
                "project": target.project,
                "instance": target.instance,
                "host_port": target.host_port,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "branch": branch_name(self.repo_root),
                "commit": commit_sha(self.repo_root),
            },
        )

    def release(self, target: RegressionTarget, claim: str) -> None:
        """Drop a reservation row while it is still the one that claim wrote."""
        self.apply_claimed(target, claim, None)

    def forget(self, slugs: Iterable[str]) -> None:
        """Drop rows for environments the owning daemon no longer has."""
        wanted = list(slugs)

        def prune(worktrees: dict) -> None:
            for slug in wanted:
                worktrees.pop(slug, None)

        self.mutate(prune)


@dataclass
class WorktreeReservation:
    """A claim on a slug and its host port, scoped to the run that made it.

    `reserve` and `complete` are the two ends of an `up` that worked. This is the
    third end: the row a failed run leaves is not wrong, only unwanted, and
    giving it back here is what frees the host port now rather than at whatever
    later `reconcile --yes` somebody happens to run. It is no longer the only way
    back -- a reservation is kept alive by the update lock its run holds, and the
    kernel drops that lock however the run ends, so an abandoned row is prunable
    by construction -- but a port reclaimed only by a command nobody ran is a
    port still allocated, and the next `up` on a fresh slug is what pays for it.

    Giving it back is only ever right while two things are true, and neither can
    be carried here from an earlier moment -- which is the whole of what an
    earlier draft of this class got wrong. It remembered "the row did not exist
    when I looked", and a concurrent `up` on the same slug could take the row
    over before the failure that consumed it. It also remembered "I am about to
    create something", set before `ensure_project_and_instance` runs its own
    listing, so a listing that never answered kept a row for an environment
    nothing had begun. A remembered fact standing in for an observation is the
    same defect twice, and every read inserted between the two moments reopens
    it. So both are read at the moment of deciding, and both live here rather
    than in `up`, for the reason round 3 named: a question every caller has to
    remember to ask is how a defect gets in.

    - The row must still be the one this reservation wrote, compared under the
      mapping lock in the same write that changes it. This is a condition on
      every end of the reservation, not just this one, so both go through the
      one writer that enforces it.
    - The daemon must say the project is absent. Present means something may
      already bind this port, and a listing that cannot answer is a "cannot
      tell" resolved the way this runner resolves every other one: keep the row.

    Holding a claim is itself the answer to "is there a row of mine here at all":
    a dry run, a target that owns no row, and a remote accessor all reserve
    without one, so neither end has to re-derive that from the run's arguments.
    """

    metadata: WorktreeMetadata
    target: RegressionTarget
    dry_run: bool = False
    claim: str | None = None

    def complete(self) -> None:
        """Stamp the environment as built, ending the reservation."""
        if self.claim is None:
            return
        self.metadata.complete(self.target, self.claim)

    def release(self, runner: Runner) -> None:
        """Give the row back if nothing came of it and nobody else has taken it."""
        if self.claim is None:
            return
        try:
            stranded = project_exists(runner, self.metadata.remote, self.target.project)
        except BaseException:
            # Asked while an `up` is already failing, so a daemon that cannot
            # answer -- or a second Ctrl-C landing here -- is an ordinary way to
            # get no answer rather than a new fault. Keeping the row is the
            # conservative half of "cannot tell", and swallowing this is what
            # lets the failure that started the unwind be the one that surfaces.
            return
        if not stranded:
            self.metadata.release(self.target, self.claim)


def allocate_worktree_port(
    metadata: WorktreeMetadata, ui_host: str, start: int, end: int, *, dry_run: bool
) -> int:
    """Pick a free host port for an environment on the daemon `metadata` describes.

    The candidate is checked against this host because it is this host that will
    bind it. There used to be an opt-out for the remote case, where the port
    belongs to another machine and a local check is meaningless -- but allocating
    from local reservations was equally meaningless there, and that is now
    refused outright, so only the owning daemon ever reaches this.
    """
    used = metadata.allocated_ports()
    for port in range(start, end + 1):
        if port in used:
            continue
        if not dry_run:
            try:
                ensure_host_port_available(ui_host, port)
            except RegressionError:
                continue
        return port
    raise RegressionError(f"No available worktree regression port in range {start}-{end}.")


@dataclass(frozen=True)
class ObservedInstance:
    """One instance Incus reported, named by the project that actually holds it.

    Incus scopes instance names per project, so a name is not an identity on its
    own; only the pair is. Carrying the project alongside the state is what lets
    a caller say which instance it means, rather than which name it saw.
    """

    project: str
    state: str


@dataclass(frozen=True)
class WorktreeEnvironment:
    """One worktree regression environment, as Incus has it and as metadata describes it.

    `project` and `instance` are always the names the naming convention derives
    from the slug, because those are the objects `delete --slug` acts on. What
    Incus was observed to hold is kept separately, so a partial or misplaced
    footprint can be reported as what it is rather than averaged into a single
    "present" flag.

    That observation is a set, not one slot. The same instance name can exist in
    several projects at once -- exactly what happens when an earlier run left one
    behind and a later one recreated it under the convention project -- and a
    single slot silently keeps whichever the listing mentioned last. Losing the
    other one is not a cosmetic omission: if the survivor is the convention-project
    instance, `reconcile` reports a footprint it can delete and prints no warning
    about the one it cannot reach.

    `in_flight` is a field and not a property, because `entry` cannot derive it.
    Whether a run still holds this slug is a fact about processes, so the caller
    observes it through `target_run_in_flight` and passes it in; see that function
    for why no recorded field is allowed to stand in for it.
    """

    slug: str
    project: str
    instance: str
    has_project: bool
    instances: tuple[ObservedInstance, ...]
    entry: dict | None
    in_flight: bool

    @property
    def exists(self) -> bool:
        """Whether Incus still holds any part of this environment.

        Either half is enough. A project whose instance is gone still owns the
        slug and still has to be reclaimed, and an instance whose project was
        never recorded still holds a host port.
        """
        return self.has_project or bool(self.instances)

    @property
    def reachable_by_slug(self) -> bool:
        """Whether `delete --slug` can name this environment at all.

        A slug here is whatever remained after stripping a known prefix off a
        name the daemon reported, so it is bounded by what Incus accepts, not by
        what the runner would mint. `delete --slug` validates its argument, so a
        suffix it rejects -- two characters, over forty, an underscore -- has no
        runner command at all. This is exactly the environment the report exists
        for: the runner did not create it, so nothing constrained its name.
        """
        return bool(SLUG_RE.match(self.slug))

    @property
    def deletable_instances(self) -> tuple[ObservedInstance, ...]:
        """The observed instances `delete --slug` would actually reach."""
        if not self.reachable_by_slug:
            return ()
        return tuple(item for item in self.instances if item.project == self.project)

    @property
    def stranded_instances(self) -> tuple[ObservedInstance, ...]:
        """The observed instances `delete --slug` would not reach.

        Either the instance lives outside the project the slug names, or the slug
        is not one the runner can name -- in which case the command reaches
        nothing and every observed instance is stranded. The two properties stay
        complementary by construction, so an instance cannot fall out of both.
        """
        if not self.reachable_by_slug:
            return self.instances
        return tuple(item for item in self.instances if item.project != self.project)

    @property
    def deletable_by_slug(self) -> bool:
        """Whether `delete --slug` reaches any part of this environment."""
        return self.reachable_by_slug and (self.has_project or bool(self.deletable_instances))

    @property
    def footprint(self) -> str:
        """What Incus was observed to hold, every part of it named.

        Reported rather than summarised: `Unknown` used to stand for an instance
        nobody had looked for, which reads as a daemon that would not answer. Each
        observed instance appears, and one outside the slug's project says where it
        is, because that is the part `delete --slug` will not reach.
        """
        if not self.instances:
            observed = ["no instance"]
        else:
            observed = [
                item.state if item.project == self.project else f"{item.state} in {item.project}"
                for item in self.instances
            ]
        return ", ".join(["project" if self.has_project else "no project", *observed])


def worktree_instances(runner: Runner, *, remote: str | None) -> dict[str, tuple[ObservedInstance, ...]]:
    """Group every worktree instance Incus reports by name, keeping each one's project.

    The project is part of the observation, not decoration. An instance living
    in a project other than the one its name implies is not reachable by
    `delete --slug`, and reporting it as if it were promises a removal that
    would silently leave it running.

    A name maps to a tuple because Incus scopes names per project, so one name can
    legitimately be several instances. Keying a single record by name kept the last
    one the listing happened to mention and dropped the rest, which is how a
    stranded instance disappeared from a report that also claimed to have
    enumerated it.
    """
    command = incus("list", *optional_remote_ref(remote), "--all-projects", "--format", "json")
    instances: dict[str, list[ObservedInstance]] = {}
    for item in runner.records(command, what="Incus instances"):
        name = item["name"]
        if name.startswith(WORKTREE_INSTANCE_PREFIX):
            instances.setdefault(name, []).append(
                ObservedInstance(
                    project=str(item.get("project") or "default"),
                    state=str(item.get("status") or "Unknown"),
                )
            )
    return {name: tuple(observed) for name, observed in instances.items()}


def worktree_environments(runner: Runner, metadata: WorktreeMetadata) -> list[WorktreeEnvironment]:
    """Every worktree regression environment, enumerated from Incus and annotated by metadata.

    Incus is the authority on what exists; `worktrees.json` only describes what
    the runner happened to record. Walking the metadata instead cannot see an
    environment created outside the runner, so a running instance stays
    invisible to every command that works from the mapping.

    Both halves of the footprint are enumerated separately. An environment whose
    project was deleted while its instance survived is neither fully present nor
    absent, and reading one half as the whole answer reports the other half as
    something it never observed.

    The daemon is taken from `metadata` rather than passed alongside it, so the
    inventory and the rows annotating it cannot come from two different
    authorities. When they did, a remote environment was annotated with the
    local row that happened to share its slug, and local-only rows were listed
    as environments the remote had lost.

    Each environment carries the names the daemon reported rather than minting
    them again from the slug. The slug is that name with a known prefix removed,
    so re-deriving it can only reproduce what was already observed -- except that
    minting validates, and a name Incus accepts is not necessarily a name the
    runner would choose. `project_name_for` therefore raised on a discovered
    two-character or over-long suffix, and `reconcile` reported nothing at all
    for the one kind of environment it exists to find. A name that was observed
    is evidence; deriving it a second time only adds a way to disagree with it.
    """
    remote = metadata.remote
    entries = metadata.rows()
    projects = {
        name[len(WORKTREE_PROJECT_PREFIX):]: name
        for name in runner.names(
            incus("project", "list", *optional_remote_ref(remote), "--format", "json"),
            what="Incus projects",
        )
        if name.startswith(WORKTREE_PROJECT_PREFIX)
    }
    instances = worktree_instances(runner, remote=remote)
    instance_names = {name[len(WORKTREE_INSTANCE_PREFIX):]: name for name in instances}
    slugs = set(entries) | set(projects) | set(instance_names)
    environments = []
    for slug in sorted(slugs):
        entry = entries.get(slug)
        entry = entry if isinstance(entry, dict) else None
        instance = instance_names.get(slug, f"{WORKTREE_INSTANCE_PREFIX}{slug}")
        project = projects.get(slug, f"{WORKTREE_PROJECT_PREFIX}{slug}")
        environments.append(
            WorktreeEnvironment(
                slug=slug,
                project=project,
                instance=instance,
                has_project=slug in projects,
                instances=instances.get(instance, ()),
                entry=entry,
                # Asked of this accessor's own daemon, so the lock read is about
                # the environment being listed and not about one that merely
                # shares its project name -- the rule the rows follow too.
                # Locks live on this machine, so they are evidence about runs
                # started on it: a remote daemon's environment may be built from
                # a machine this one cannot see, and it has no local row to act
                # on, so no answer is claimed for it at all.
                in_flight=metadata.owned
                and target_run_in_flight(metadata.repo_root, metadata.remote, project),
            )
        )
    return environments


def describe_worktree_entry(entry: dict | None) -> str:
    if entry is None:
        return "no runner metadata"
    parts = []
    if isinstance(entry.get("host_port"), int):
        parts.append(f"port {entry['host_port']}")
    branch = str(entry.get("branch") or "").strip()
    commit = str(entry.get("commit") or "").strip()
    if branch:
        parts.append(f"branch {branch}")
    elif commit:
        parts.append(f"detached at {commit[:12]}")
    else:
        parts.append("no branch or commit recorded")
    path = str(entry.get("path") or "").strip()
    if path:
        # Provenance only. This is the checkout the runner was invoked from, not
        # the environment's identity: several environments created from one
        # checkout all record the same path, so its presence on disk says
        # nothing about whether any of them is still wanted.
        parts.append(f"created from {path}")
    return ", ".join(parts)


def target_slug(args: argparse.Namespace, repo_root: Path) -> str:
    """The slug this invocation names, without consulting the mapping.

    Split out because a caller may need the environment's identity before it is
    allowed to read ports -- `up` names its update lock from this and allocates
    inside it. Identity is derivable from the arguments alone, so nothing here
    touches `worktrees.json`.
    """
    if args.target not in TARGETS:
        raise RegressionError(f"target must be one of: {', '.join(sorted(TARGETS))}")
    if args.target == MASTER_TARGET:
        return "master"
    return worktree_slug(repo_root, args.slug)


def resolve_target(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    dry_run: bool,
    allocate_port: bool = True,
    slug: str | None = None,
) -> RegressionTarget:
    # An identity already observed is passed in rather than observed again: a
    # slug derived from the checkout's branch is a question that can be answered
    # twice differently, and `up` has to lock the same environment it builds.
    slug = slug or target_slug(args, repo_root)
    ui_host = args.ui_host or host_bind_env()
    ui_port = args.ui_port
    if args.target == MASTER_TARGET:
        host_port = args.host_port or env_int("REGRESSION_PORT") or DEFAULT_MASTER_HOST_PORT
    else:
        metadata = WorktreeMetadata(repo_root, args.remote)
        host_port = args.host_port or metadata.port_for(slug)
        if host_port is None and allocate_port:
            if not metadata.owned:
                # The port is a "cannot tell", so it is asked for rather than
                # guessed. Allocation reads this machine's reservations, which
                # say nothing about which of another daemon's ports are free,
                # and no one has asked that daemon. Answering anyway is how a
                # remote environment came to be handed a live local
                # environment's port.
                raise RegressionError(
                    f"--host-port is required for a worktree environment on remote {args.remote}.\n"
                    "Worktree ports are allocated from this machine's metadata, which is no evidence "
                    "about another daemon's ports."
                )
            host_port = allocate_worktree_port(
                metadata,
                ui_host,
                args.worktree_port_start,
                args.worktree_port_end,
                dry_run=dry_run,
            )
        if host_port is None:
            host_port = 0
    return RegressionTarget(
        target=args.target,
        slug=slug,
        project=project_name_for(args.target, slug),
        instance=instance_name_for(args.target, slug),
        host_port=host_port,
        ui_host=ui_host,
        ui_port=ui_port,
    )


def project_create_config(target: RegressionTarget) -> list[str]:
    return [
        "features.images=false",
        "features.profiles=true",
        "features.storage.volumes=true",
        "restricted=true",
        "restricted.devices.proxy=allow",
        "limits.instances=1",
        "limits.containers=1",
        f"user.avibe_regression.target={target.target}",
        f"user.avibe_regression.slug={target.slug}",
        f"user.avibe_regression.instance={target.instance}",
        f"user.avibe_regression.host_port={target.host_port}",
    ]


def profile_yaml(storage_pool: str, network: str, cpus: str, memory: str, disk: str, processes: str) -> str:
    return textwrap.dedent(
        f"""\
        config:
          limits.cpu: "{cpus}"
          limits.memory: "{memory}"
          limits.processes: "{processes}"
        description: Avibe regression profile
        devices:
          eth0:
            name: eth0
            network: {network}
            type: nic
          root:
            path: /
            pool: {storage_pool}
            size: {disk}
            type: disk
        name: default
        """
    )


def regression_service_unit() -> str:
    return textwrap.dedent(
        f"""\
        [Unit]
        Description=Avibe regression service
        Wants=network-online.target
        After=network-online.target

        [Service]
        Type=simple
        User={SERVICE_USER}
        Group={SERVICE_USER}
        WorkingDirectory={SOURCE_DIR}
        Environment=HOME={SERVICE_HOME}
        Environment=AVIBE_HOME=
        Environment=VIBE_DEPLOYMENT_ENV=regression
        Environment=VIBE_BUILD_METADATA_PATH={METADATA_PATH}
        Environment=AVIBE_ALLOW_DEV_STATE_MIGRATION=1
        Environment=VIBE_INTERNAL_DISPATCH_SOCKET={INTERNAL_DISPATCH_SOCKET}
        Environment=PYTHONUNBUFFERED=1
        Environment=PATH={VENV_DIR}/bin:{SERVICE_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin
        EnvironmentFile=-/etc/avibe-regression.env
        ExecStart={VENV_DIR}/bin/python scripts/incus_regression_supervisor.py
        Delegate=yes
        CPUAccounting=yes
        IOAccounting=yes
        MemoryAccounting=yes
        TasksAccounting=yes
        Restart=on-failure
        RestartSec=2
        TimeoutStopSec=60

        [Install]
        WantedBy=multi-user.target
        """
    ).rstrip()


def cloud_init_user_data() -> str:
    service = regression_service_unit()
    helper = textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        echo "service={SERVICE_NAME}"
        echo "source={SOURCE_DIR}"
        echo "home={AVIBE_HOME}"
        echo "metadata={METADATA_PATH}"
        """
    ).rstrip()
    lines = [
        "#cloud-config",
        "package_update: true",
        "packages:",
        "  - bash",
        "  - ca-certificates",
        "  - curl",
        "  - git",
        "  - build-essential",
        "  - python3",
        "  - python3-pip",
        "  - python3-venv",
        "  - rsync",
        "  - sudo",
        "users:",
        f"  - name: {SERVICE_USER}",
        "    groups: sudo",
        "    shell: /bin/bash",
        "    sudo: ALL=(ALL) NOPASSWD:ALL",
        "    lock_passwd: true",
        "write_files:",
        f"  - path: /etc/systemd/system/{SERVICE_NAME}",
        "    owner: root:root",
        "    permissions: '0644'",
        "    content: |",
        yaml_block(service),
        "  - path: /usr/local/bin/avibe-regression-info",
        "    owner: root:root",
        "    permissions: '0755'",
        "    content: |",
        yaml_block(helper),
        "runcmd:",
        f"  - [mkdir, -p, {SOURCE_DIR}, {VENV_DIR}, {METADATA_DIR}, {AVIBE_HOME}]",
        f"  - [chown, -R, {SERVICE_USER}:{SERVICE_USER}, {SERVICE_HOME}, /opt/avibe, {METADATA_DIR}]",
        f"  - [ln, -sfn, {AVIBE_HOME}, {LEGACY_HOME}]",
        "  - [systemctl, daemon-reload]",
        f'final_message: "Avibe regression base is ready."',
    ]
    return "\n".join(lines)


def yaml_block(value: str, indent: int = 6) -> str:
    prefix = " " * indent
    return "\n".join(prefix + line if line else prefix for line in value.splitlines())


def ui_device_endpoints(target: RegressionTarget) -> dict[str, str]:
    """The `listen`/`connect` pair the `ui` proxy device must forward.

    One owner for these strings, so the create, update, and compare paths cannot
    drift into disagreeing about what "already correct" means.
    """
    return {
        "listen": tcp_endpoint(target.ui_host, target.host_port),
        "connect": f"tcp:127.0.0.1:{target.ui_port}",
    }


def proxy_device_args(target: RegressionTarget, *, remote: str | None = None) -> list[str]:
    endpoints = ui_device_endpoints(target)
    return [
        "config",
        "device",
        "add",
        remote_ref(remote, target.instance),
        "ui",
        "proxy",
        f"listen={endpoints['listen']}",
        f"connect={endpoints['connect']}",
    ]


def ui_device_present(runner: Runner, target: RegressionTarget, *, remote: str | None) -> bool:
    """Whether the instance has a `ui` device, from a listing the daemon completed.

    `config device get` cannot answer this: it exits non-zero both for a device
    that is genuinely absent and for a daemon it could not reach. `config device
    list` can, because it exits zero only after the daemon enumerated the
    instance's devices -- so a failure here means "cannot tell" and is raised
    rather than being read as "there is nothing there".
    """
    result = runner.run(
        incus("config", "device", "list", remote_ref(remote, target.instance), project=target.project),
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        raise RegressionError(
            f"Could not list the devices of {target.instance}: {daemon_failure_detail(result)}\n"
            f"{DAEMON_UNREACHABLE_HINT}"
        )
    return "ui" in (result.stdout or "").split()


def observed_ui_endpoints(runner: Runner, target: RegressionTarget, *, remote: str | None) -> dict[str, str] | None:
    """The endpoints the instance's `ui` device forwards, or None when it has none.

    None is a confirmed absence, never an unanswered question: a daemon that
    will not say raises instead. The caller is about to change this device, and
    silence is not evidence that there is nothing there to lose.
    """
    if not ui_device_present(runner, target, remote=remote):
        return None
    observed: dict[str, str] = {}
    for key in ("listen", "connect"):
        result = runner.run(
            incus(
                "config",
                "device",
                "get",
                remote_ref(remote, target.instance),
                "ui",
                key,
                project=target.project,
            ),
            check=False,
            capture=True,
        )
        value = (result.stdout or "").strip()
        if result.returncode != 0 or not value:
            raise RegressionError(
                f"Incus lists a `ui` device on {target.instance} but would not report its {key}: "
                f"{daemon_failure_detail(result)}\n{DAEMON_UNREACHABLE_HINT}"
            )
        observed[key] = value
    return observed


def ensure_proxy_device(runner: Runner, target: RegressionTarget, *, remote: str | None) -> None:
    """Make the instance's `ui` proxy device forward the target's endpoints.

    The device is added only when the daemon reported it missing and updated in
    place only when the daemon reported what it currently forwards. Removing it
    first was the destructive way to do this: a failed `add` aborted the run
    with the instance left holding no `ui` device at all, so a routine re-run of
    `up` could take the Web UI away -- and it ran precisely when the daemon was
    already misbehaving, because an unreadable device was treated as an absent
    one. An unreadable device now aborts before anything is mutated.
    """
    desired = ui_device_endpoints(target)
    observed = observed_ui_endpoints(runner, target, remote=remote)
    if observed is None:
        runner.run(incus(*proxy_device_args(target, remote=remote), project=target.project))
        return
    if observed == desired:
        print(f"ui proxy device already forwards {desired['listen']} -> {desired['connect']}")
        return
    runner.run(
        incus(
            "config",
            "device",
            "set",
            remote_ref(remote, target.instance),
            "ui",
            f"listen={desired['listen']}",
            f"connect={desired['connect']}",
            project=target.project,
        )
    )


def ensure_project_and_instance(
    runner: Runner,
    target: RegressionTarget,
    *,
    image: str,
    storage_pool: str,
    network: str,
    cpus: str,
    memory: str,
    disk: str,
    processes: str,
    remote: str | None,
) -> None:
    if not project_exists(runner, remote, target.project):
        command = incus("project", "create", remote_ref(remote, target.project))
        for item in project_create_config(target):
            command.extend(["--config", item])
        runner.run(command)
        runner.run(
            incus("profile", "edit", remote_ref(remote, "default"), project=target.project),
            input_text=profile_yaml(storage_pool, network, cpus, memory, disk, processes),
        )
    if not instance_exists(runner, remote, target.project, target.instance):
        runner.run(
            incus(
                "init",
                remote_ref(remote, image) if remote and ":" not in image else image,
                remote_ref(remote, target.instance),
                "--profile",
                "default",
                "--config",
                f"cloud-init.user-data={cloud_init_user_data()}",
                project=target.project,
            )
        )
    ensure_proxy_device(runner, target, remote=remote)
    runner.run(incus("start", remote_ref(remote, target.instance), project=target.project), check=False)
    runner.run(
        root_exec(
            target,
            (
                "if command -v cloud-init >/dev/null 2>&1; then cloud-init status --wait || true; fi; "
                f"mkdir -p {SOURCE_DIR} {VENV_DIR} {METADATA_DIR} {AVIBE_HOME}; "
                f"chown -R {SERVICE_USER}:{SERVICE_USER} {SERVICE_HOME} /opt/avibe {METADATA_DIR}; "
                f"ln -sfn {AVIBE_HOME} {LEGACY_HOME}; "
                "systemctl daemon-reload"
            ),
            remote=remote,
        )
    )
    runner.run(
        root_exec(
            target,
            f"cat > /etc/systemd/system/{SERVICE_NAME} <<'EOF'\n{regression_service_unit()}\nEOF\n"
            "systemctl daemon-reload",
            remote=remote,
        )
    )


def tenant_exec(target: RegressionTarget, command: str, *args: str, remote: str | None = None) -> list[str]:
    bash_command = (
        "set -a; [ ! -f /etc/avibe-regression.env ] || . /etc/avibe-regression.env; "
        "VIBE_DEPLOYMENT_ENV=regression; AVIBE_ALLOW_DEV_STATE_MIGRATION=1; "
        f"VIBE_INTERNAL_DISPATCH_SOCKET={shlex.quote(INTERNAL_DISPATCH_SOCKET)}; "
        f"set +a; cd {shlex.quote(SOURCE_DIR)} && {command}"
    )
    return incus(
        "exec",
        remote_ref(remote, target.instance),
        "--",
        "sudo",
        "-H",
        "-u",
        SERVICE_USER,
        "--",
        "bash",
        "-lc",
        bash_command,
        "bash",
        *args,
        project=target.project,
    )


def root_exec(target: RegressionTarget, command: str, *, remote: str | None = None) -> list[str]:
    return incus("exec", remote_ref(remote, target.instance), "--", "bash", "-lc", command, project=target.project)


def source_excludes(*, include_ui_dist: bool = False) -> tuple[str, ...]:
    """Path patterns dropped from the deployed source tree.

    A bare pattern matches that name at any depth. A pattern with a leading
    ``/`` is anchored at the repository root, which is what keeps ``/dist``
    (the Python build output) from also swallowing ``ui/dist``, whose fate
    belongs to ``include_ui_dist``.
    """
    excludes = [
        ".git",
        ".runtime",
        ".worktrees",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "node_modules",
        "ui/node_modules",
        "ui/.vite",
        "_tmp",
        "tmp",
        "logs",
        "/dist",
    ]
    if not include_ui_dist:
        excludes.append("ui/dist")
    return tuple(excludes)


def is_env_file(relative: str) -> bool:
    return any(part == ".env" or part.startswith(".env.") for part in relative.split("/"))


def should_exclude(relative: str, *, include_ui_dist: bool = False) -> bool:
    if is_env_file(relative):
        return True
    parts = relative.split("/")
    for pattern in source_excludes(include_ui_dist=include_ui_dist):
        if pattern.startswith("/"):
            anchored = pattern[1:]
            if relative == anchored or relative.startswith(anchored + "/"):
                return True
            continue
        pattern_parts = pattern.split("/")
        if relative == pattern or relative.startswith(pattern + "/"):
            return True
        if len(pattern_parts) == 1 and pattern in parts:
            return True
    return False


def is_virtualenv_dir(path: Path) -> bool:
    """Whether ``path`` is a Python virtualenv root, whatever it is named.

    ``pyvenv.cfg`` is the marker the interpreter itself writes, so this covers
    ``venv``, ``.venv``, ``env`` and anything else a contributor happens to
    use. Naming them one by one is how a 600 MB tree of host-native binaries
    ends up shipped into a Linux container.
    """
    return (path / "pyvenv.cfg").is_file()


def iter_source_entries(repo_root: Path, *, include_ui_dist: bool = False) -> Iterator[tuple[Path, str]]:
    """Yield ``(path, arcname)`` for everything that belongs in the source tar.

    Excluded directories are pruned during the walk rather than filtered after
    it: ``node_modules`` and a virtualenv together hold well over a hundred
    thousand paths that would otherwise be stat'ed just to be discarded.
    """
    def walk(current: Path) -> Iterator[tuple[Path, str]]:
        for entry in sorted(current.iterdir()):
            relative = entry.relative_to(repo_root).as_posix()
            if should_exclude(relative, include_ui_dist=include_ui_dist):
                continue
            is_dir = entry.is_dir() and not entry.is_symlink()
            if is_dir and is_virtualenv_dir(entry):
                continue
            yield entry, relative
            if is_dir:
                yield from walk(entry)

    yield from walk(repo_root)


def build_source_tar(repo_root: Path, *, include_ui_dist: bool = False) -> bytes:
    with tempfile.TemporaryFile() as fh:
        with tarfile.open(fileobj=fh, mode="w") as tar:
            for path, relative in iter_source_entries(repo_root, include_ui_dist=include_ui_dist):
                tar.add(path, arcname=relative, recursive=False)
        fh.seek(0)
        return fh.read()


def sync_source(
    runner: Runner,
    target: RegressionTarget,
    repo_root: Path,
    *,
    remote: str | None,
    clean: bool,
    include_ui_dist: bool = False,
) -> None:
    quoted_source = shlex.quote(SOURCE_DIR)
    if clean:
        wipe = f"find {quoted_source} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +"
    else:
        # Everything stale still goes, but the UI dependency tree and its build
        # output stay: they are the ~470 MB that a full wipe forces ``npm ci``
        # and ``npm run build`` to recreate on every update whether or not the
        # front end changed. Whether they are still valid is a fingerprint
        # question, answered in update_dependencies_and_build.
        #
        # Preservation covers only what the archive does not carry. ``tar``
        # extracts over what is already there and never deletes, so keeping a
        # directory the host also ships would leave the files the host deleted
        # -- a stable-name public asset, a manifest -- served alongside the new
        # bundle. Whatever the archive supplies has to end up equal to the
        # host's copy, which means the old one goes first. Which of these the
        # archive supplies is read off the exclusion table rather than restated
        # here, so the two cannot drift.
        quoted_ui = shlex.quote(f"{SOURCE_DIR}/ui")
        keep = " ".join(
            f"! -name {shlex.quote(name)}"
            for name in UI_NON_SOURCE_DIRS
            if should_exclude(f"ui/{name}", include_ui_dist=include_ui_dist)
        )
        wipe = (
            f"find {quoted_source} -mindepth 1 -maxdepth 1 ! -name ui -exec rm -rf {{}} + && "
            f"if [ -d {quoted_ui} ]; then find {quoted_ui} -mindepth 1 -maxdepth 1 {keep} -exec rm -rf {{}} +; fi"
        )
    runner.run(root_exec(target, f"mkdir -p {quoted_source} && {wipe}", remote=remote))
    runner.run(root_exec(target, f"mkdir -p {quoted_source} && chown -R {SERVICE_USER}:{SERVICE_USER} /opt/avibe", remote=remote))
    tar_bytes = b"" if runner.dry_run else build_source_tar(repo_root, include_ui_dist=include_ui_dist)
    runner.run(
        incus("exec", remote_ref(remote, target.instance), "--", "tar", "-C", SOURCE_DIR, "-xf", "-", project=target.project),
        input_bytes=tar_bytes,
    )
    runner.run(root_exec(target, f"chown -R {SERVICE_USER}:{SERVICE_USER} {shlex.quote(SOURCE_DIR)}", remote=remote))


def stop_service_for_update(runner: Runner, target: RegressionTarget, *, remote: str | None) -> None:
    runner.run(root_exec(target, f"systemctl stop {SERVICE_NAME} || true", remote=remote), check=False)


def restart_service_after_failed_update(runner: Runner, target: RegressionTarget, *, remote: str | None) -> None:
    runner.run(root_exec(target, f"systemctl start {SERVICE_NAME} || true", remote=remote), check=False)


def migrate_legacy_backend_runtimes(runner: Runner, target: RegressionTarget, *, remote: str | None) -> None:
    """Move pre-#545 root-global backends into the service user's home.

    Long-lived regression instances keep their original base image when ``up``
    syncs newer source. Instances created before #545 therefore still resolve
    root-owned CLIs under ``/usr`` even though current base images install
    self-updatable copies under ``/home/avibe``. Only migrate a backend when its
    user-owned entrypoint is absent and the corresponding legacy binary exists.
    """
    runner.run(
        tenant_exec(
            target,
            textwrap.dedent(
                f"""\
                set -euo pipefail
                user_bin={shlex.quote(f"{SERVICE_HOME}/.local/bin")}
                npm_prefix={shlex.quote(f"{SERVICE_HOME}/.npm-global")}
                mkdir -p "$user_bin"

                npm_packages=()
                if [ ! -x "$user_bin/claude" ]; then
                    if [ -x "$npm_prefix/bin/claude" ]; then
                        ln -sfn "$npm_prefix/bin/claude" "$user_bin/claude"
                    elif [ -x /usr/bin/claude ] || [ -x /usr/local/bin/claude ]; then
                        npm_packages+=("@anthropic-ai/claude-code")
                    fi
                fi
                if [ ! -x "$user_bin/codex" ]; then
                    if [ -x "$npm_prefix/bin/codex" ]; then
                        ln -sfn "$npm_prefix/bin/codex" "$user_bin/codex"
                    elif [ -x /usr/bin/codex ] || [ -x /usr/local/bin/codex ]; then
                        npm_packages+=("@openai/codex")
                    fi
                fi
                if [ "${{#npm_packages[@]}}" -gt 0 ]; then
                    echo "Migrating legacy npm-installed agent backends into $npm_prefix"
                    npm config set prefix "$npm_prefix" --location=user
                    npm install --global --prefix "$npm_prefix" "${{npm_packages[@]}}"
                    for backend in claude codex; do
                        if [ -x "$npm_prefix/bin/$backend" ]; then
                            ln -sfn "$npm_prefix/bin/$backend" "$user_bin/$backend"
                        fi
                    done
                fi

                if [ ! -x "$user_bin/opencode" ]; then
                    if [ -x "{SERVICE_HOME}/.opencode/bin/opencode" ]; then
                        ln -sfn "{SERVICE_HOME}/.opencode/bin/opencode" "$user_bin/opencode"
                    elif [ -x /usr/local/bin/opencode ] || [ -x /usr/bin/opencode ]; then
                        echo "Migrating legacy OpenCode into {SERVICE_HOME}/.opencode"
                        curl -fsSL --retry 3 --retry-delay 2 --retry-all-errors https://opencode.ai/install \
                            | HOME={shlex.quote(SERVICE_HOME)} bash -s -- --no-modify-path
                        test -x "{SERVICE_HOME}/.opencode/bin/opencode"
                        ln -sfn "{SERVICE_HOME}/.opencode/bin/opencode" "$user_bin/opencode"
                    fi
                fi
                """
            ),
            remote=remote,
        )
    )


def file_hash(repo_root: Path, relative_paths: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for relative in relative_paths:
        path = repo_root / relative
        digest.update(relative.encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
        else:
            digest.update(b"<missing>")
    return digest.hexdigest()


def compute_fingerprints(repo_root: Path) -> dict:
    # ``ui_source`` covers the files the UI build reads, rather than a list of
    # the build inputs we happened to think of. The list form silently missed
    # ``postcss.config.js``, ``eslint.config.js`` and ``ui/scripts/``; a build
    # input added tomorrow is covered without anyone remembering to extend a
    # literal, including the ones that live outside ``ui/``.
    show_runtime_env = regression_show_runtime_env(None, SERVICE_HOME)
    return {
        "python": file_hash(repo_root, ["pyproject.toml", "uv.lock"]),
        "ui_deps": file_hash(repo_root, ["ui/package.json", "ui/package-lock.json"]),
        "ui_source": ui_source_hash(repo_root),
        "show_runtime": "|".join(
            [
                show_runtime_env["VIBE_SHOW_RUNTIME_SOURCE"],
                show_runtime_env.get("VIBE_SHOW_RUNTIME_ARCHIVE_PATH", ""),
            ]
        ),
    }


def tree_hash(root: Path, *, prune: Sequence[str] = ()) -> str:
    """Content hash of every file under ``root``, skipping pruned directories.

    ``prune`` names directories, at any depth, that hold build outputs or
    installed dependencies rather than sources.
    """
    digest = hashlib.sha256()
    if not root.exists():
        return "<missing>"
    pruned = set(prune)
    files: list[Path] = []
    stack = [root]
    while stack:
        for entry in stack.pop().iterdir():
            if entry.is_dir() and not entry.is_symlink():
                if entry.name not in pruned:
                    stack.append(entry)
            elif entry.is_file():
                files.append(entry)
    for path in sorted(files):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def ui_external_build_inputs(repo_root: Path) -> list[str]:
    """Repo-relative paths outside ``ui/`` that the UI bundle is built from.

    ``ui_source`` licenses skipping ``npm run build``, so it has to cover the
    files the build reads. ``ui/`` is where most of them live, not what they
    are: ``ui/src/lib/messageTypes.ts`` imports the repo-root
    ``vibe/message_types.json`` and Vite inlines it into the browser bundle, so
    a commit touching only that catalog changes the artifact while leaving a
    ``ui/``-only hash identical -- the backend would get the new message-type
    policies and the front end would keep applying the old ones.

    The repository already declares this set and already keeps the declaration
    honest, so this reads it rather than deriving it a second way. The
    ``ui-builder`` stage builds the UI from a context holding only ``ui/``, so
    every escaping input needs its own ``COPY`` there, and
    ``ui/scripts/validate-out-of-tree-imports.mjs`` -- part of ``npm run
    build``, which CI runs on every pull request -- fails the build when those
    ``COPY`` lines and the real imports disagree. An empty result is therefore a
    real answer rather than a scan that broke: it means the stage declares
    nothing outside ``ui/``.
    """
    stage = next(
        (
            block
            for block in re.split(r"^FROM ", (repo_root / "Dockerfile").read_text(encoding="utf-8"), flags=re.MULTILINE)
            if re.match(r"^\S+\s+AS\s+ui-builder\b", block, flags=re.IGNORECASE)
        ),
        None,
    )
    if stage is None:
        # Failing here beats returning a smaller input set than the build has:
        # a fingerprint missing an input reads back as "the UI is unchanged"
        # and skips the rebuild for good.
        raise RuntimeError("Dockerfile no longer declares a ui-builder stage, so the UI build inputs are unknown")
    found = set()
    for arguments in re.findall(r"^COPY\s+(.+)$", stage, flags=re.MULTILINE):
        sources = [word for word in arguments.split()[:-1] if not word.startswith("--")]
        found.update(source.rstrip("/") for source in sources if not source.startswith("ui/"))
    return sorted(found)


def ui_source_hash(repo_root: Path) -> str:
    """The UI build's whole input set: the ``ui/`` tree plus what escapes it."""
    digest = hashlib.sha256()
    digest.update(tree_hash(repo_root / "ui", prune=UI_NON_SOURCE_DIRS).encode("utf-8"))
    for relative in ui_external_build_inputs(repo_root):
        path = repo_root / relative
        digest.update(relative.encode("utf-8"))
        digest.update((tree_hash(path) if path.is_dir() else file_hash(repo_root, [relative])).encode("utf-8"))
    return digest.hexdigest()


def reconciled_fingerprints(previous: dict, current: dict, reconciled: Container[str]) -> dict:
    """The fingerprints to record: what the artifacts on disk were built from.

    A recorded fingerprint is read back as "the artifact in the instance was
    produced from this input", which is what licenses a later update to skip
    rebuilding it. That is only the same thing as "this input was synced" when
    the run actually rebuilt the artifact. ``--no-build-ui`` skips the UI
    entirely, so recording the synced source there would claim a dependency
    tree and a bundle the instance never installed or built.

    A key the run did not reconcile therefore keeps whatever the last
    reconciliation recorded, so the next update still sees the difference. A key
    with no previous value stays absent, which also reads as "rebuild".
    """
    merged = {}
    for key, value in current.items():
        if key in reconciled:
            merged[key] = value
        elif key in previous:
            merged[key] = previous[key]
    return merged


def invalidate_fingerprints(runner: Runner, target: RegressionTarget, *, remote: str | None) -> None:
    """Drop the recorded fingerprints before anything starts rebuilding.

    A recorded fingerprint claims "the artifact in the instance was produced
    from this input". The moment a rebuild starts, that claim stops holding for
    the artifact it names: ``npm ci`` empties ``ui/node_modules``, ``npm run
    build`` writes into ``ui/dist``, ``pip install`` rewrites the venv. A run
    that dies mid-build would otherwise leave the old claim on disk, and if the
    next update syncs the same inputs again -- a rollback, or a rerun after
    fixing the environment rather than the source -- that stale claim licenses
    skipping the very rebuild that failed.

    Recording nothing reads as "rebuild everything", which is the honest answer
    while a build is in flight. ``write_metadata`` records the real
    fingerprints once the artifacts exist.
    """
    runner.run(
        root_exec(
            target,
            f"mkdir -p {METADATA_DIR} && cat > {FINGERPRINT_PATH} <<'EOF'\n{{}}\nEOF",
            remote=remote,
        )
    )


def write_metadata(runner: Runner, target: RegressionTarget, repo_root: Path, fingerprints: dict, *, remote: str | None) -> None:
    payload = {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "target": target.target,
        "slug": target.slug,
        "project": target.project,
        "instance": target.instance,
        "repo_root": str(repo_root),
        "branch": branch_name(repo_root),
        "commit": commit_sha(repo_root),
        "dirty": is_dirty(repo_root),
        "fingerprints": fingerprints,
    }
    encoded = json.dumps(payload, indent=2)
    command = f"mkdir -p {METADATA_DIR} && cat > {METADATA_PATH} <<'EOF'\n{encoded}\nEOF\ncat > {FINGERPRINT_PATH} <<'EOF'\n{json.dumps(fingerprints, indent=2)}\nEOF"
    runner.run(root_exec(target, command, remote=remote))


def runtime_env_payload(repo_root: Path | None = None) -> bytes:
    scm_version = "0.0.0.dev0"
    if repo_root is not None:
        sha = commit_sha(repo_root)
        if sha:
            scm_version = f"0.0.0.dev0+{sha[:12]}"
    mappings = {
        "SETUPTOOLS_SCM_PRETEND_VERSION": scm_version,
        "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AVIBE_OS": scm_version,
        "REGRESSION_UI_HOST": CONTAINER_UI_HOST,
        "AVIBE_ALLOW_DEV_STATE_MIGRATION": "1",
        **regression_show_runtime_env(None, SERVICE_HOME),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
        "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", ""),
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
        "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL", ""),
        "OPENAI_API_BASE": os.environ.get("OPENAI_API_BASE", ""),
    }
    for key, value in os.environ.items():
        if key in {"REGRESSION_UI_HOST", "REGRESSION_VOICE_REALTIME_ENABLED"}:
            continue
        if key.startswith(ENV_PREFIX):
            mappings[key] = value
    lines = [f"{key}={shlex.quote(value)}" for key, value in mappings.items() if value]
    return ("\n".join(lines) + "\n").encode("utf-8")


def required_platform_seed_envs() -> tuple[str, ...]:
    required = [
        "REGRESSION_SLACK_BOT_TOKEN",
        "REGRESSION_SLACK_APP_TOKEN",
        "REGRESSION_DISCORD_BOT_TOKEN",
        "REGRESSION_FEISHU_APP_ID",
        "REGRESSION_FEISHU_APP_SECRET",
    ]
    return tuple(required)


def env_value(key: str) -> str:
    value = os.environ.get(key, "")
    return value.strip()


def require_runtime_seed_env() -> None:
    required = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", *required_platform_seed_envs())
    missing = [key for key in required if not env_value(key)]
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(f"Missing required regression seed environment variables: {joined}")


def write_runtime_env(runner: Runner, target: RegressionTarget, *, repo_root: Path | None = None, remote: str | None) -> None:
    runner.run(
        incus(
            "exec",
            remote_ref(remote, target.instance),
            "--",
            "bash",
            "-lc",
            f"cat > /etc/avibe-regression.env && chown root:{SERVICE_USER} /etc/avibe-regression.env && chmod 0640 /etc/avibe-regression.env",
            project=target.project,
        ),
        input_bytes=b"" if runner.dry_run else runtime_env_payload(repo_root),
    )


def read_existing_fingerprints(runner: Runner, target: RegressionTarget, *, remote: str | None) -> dict:
    if runner.dry_run:
        return {}
    result = runner.run(
        root_exec(target, f"test -f {FINGERPRINT_PATH} && cat {FINGERPRINT_PATH} || true", remote=remote),
        capture=True,
        check=False,
    )
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def should_seed_state(runner: Runner, target: RegressionTarget, *, reset_mode: str, remote: str | None) -> bool:
    if runner.dry_run or reset_mode != "none":
        return True
    result = runner.run(
        root_exec(target, f"test -f {AVIBE_HOME}/config/config.json", remote=remote),
        check=False,
    )
    return result.returncode != 0


def remote_pairing_probe_script() -> str:
    return textwrap.dedent(f"""
        import json
        import os
        from pathlib import Path

        default_paths = [{str(AVIBE_HOME + "/config/config.json")!r}, {str(LEGACY_HOME + "/config/config.json")!r}]
        env_paths = os.environ.get("AVIBE_REMOTE_PAIRING_CONFIG_PATHS")
        if env_paths:
            paths = [Path(path) for path in env_paths.split(os.pathsep) if path]
        else:
            legacy_env_path = os.environ.get("AVIBE_REMOTE_PAIRING_CONFIG_PATH")
            paths = [Path(legacy_env_path)] if legacy_env_path else [Path(path) for path in default_paths]

        saw_config = False
        for path in paths:
            if not path.exists():
                continue
            saw_config = True
            try:
                payload = json.loads(path.read_text())
            except Exception:
                print(json.dumps({{"state": "unknown", "path": str(path)}}))
                raise SystemExit(0)

            remote_access = payload.get("remote_access") if isinstance(payload, dict) else None
            if not isinstance(remote_access, dict):
                continue

            vibe_cloud = remote_access.get("vibe_cloud")
            if not isinstance(vibe_cloud, dict):
                vibe_cloud = {{}}
            paired = bool(
                remote_access.get("enabled")
                or remote_access.get("public_url")
                or remote_access.get("tunnel_id")
                or remote_access.get("credentials_file")
                or remote_access.get("cloudflared_config")
                or vibe_cloud.get("enabled")
                or vibe_cloud.get("public_url")
                or vibe_cloud.get("instance_id")
                or vibe_cloud.get("tunnel_token")
                or vibe_cloud.get("instance_secret")
                or vibe_cloud.get("session_secret")
            )
            if paired:
                print(json.dumps({{"state": "paired", "path": str(path)}}))
                raise SystemExit(0)

        if not saw_config:
            print(json.dumps({{"state": "unpaired"}}))
            raise SystemExit(0)
        print(json.dumps({{"state": "unpaired"}}))
    """).strip()


def target_remote_pairing_state(runner: Runner, target: RegressionTarget, *, remote: str | None) -> bool | None:
    if runner.dry_run:
        return False
    script = remote_pairing_probe_script()
    result = runner.run(
        root_exec(target, f"python3 - <<'PY'\n{script}\nPY", remote=remote),
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    state = payload.get("state")
    if state == "paired":
        return True
    if state == "unpaired":
        return False
    return None


def guard_paired_master_reset(
    runner: Runner,
    target: RegressionTarget,
    *,
    reset_mode: str,
    allow_reset_paired_master: bool,
    remote: str | None,
) -> None:
    if reset_mode == "none" or target.target != MASTER_TARGET or allow_reset_paired_master:
        return
    pairing_state = target_remote_pairing_state(runner, target, remote=remote)
    if pairing_state is False:
        return
    raise RegressionError(
        "Refusing to reset the master regression environment because Avibe Cloud pairing "
        "state is present or could not be verified safely. Re-run with "
        "--allow-reset-paired-master only if you intentionally want to pair it again afterward."
    )


def run_prepare_state(runner: Runner, target: RegressionTarget, *, reset_mode: str, remote: str | None) -> None:
    if not should_seed_state(runner, target, reset_mode=reset_mode, remote=remote):
        print("Existing Avibe state found; skipping regression state seed.")
        return
    runner.run(root_exec(target, f"rm -rf /home/{SERVICE_USER}/.regression-seed", remote=remote))
    runner.run(
        tenant_exec(
            target,
            f"{VENV_DIR}/bin/python scripts/prepare_regression.py --output-root /home/{SERVICE_USER}/.regression-seed --reset-mode {shlex.quote(reset_mode)}",
            remote=remote,
        )
    )
    if reset_mode == "config":
        runner.run(
            root_exec(
                target,
                f"rm -rf {AVIBE_HOME}/config {AVIBE_HOME}/state {AVIBE_HOME}/runtime",
                remote=remote,
            )
        )
    elif reset_mode == "all":
        runner.run(
            root_exec(
                target,
                "rm -rf "
                f"{AVIBE_HOME} {LEGACY_HOME} "
                f"{SERVICE_HOME}/.claude {SERVICE_HOME}/.claude.json {SERVICE_HOME}/.codex "
                f"{SERVICE_HOME}/.config/opencode {SERVICE_HOME}/.local/share/opencode",
                remote=remote,
            )
        )
    runner.run(
        root_exec(
            target,
            f"mkdir -p {AVIBE_HOME} && "
            f"cp -a /home/{SERVICE_USER}/.regression-seed/home/. {SERVICE_HOME}/ && "
            f"chown -R {SERVICE_USER}:{SERVICE_USER} {SERVICE_HOME} && "
            f"ln -sfn {AVIBE_HOME} {LEGACY_HOME} && chown -h {SERVICE_USER}:{SERVICE_USER} {LEGACY_HOME}",
            remote=remote,
        )
    )


def instance_ui_dist_exists(runner: Runner, target: RegressionTarget, *, remote: str | None) -> bool:
    result = runner.run(
        tenant_exec(target, "test -d ui/dist && test -f ui/dist/index.html", remote=remote),
        check=False,
    )
    return result.returncode == 0


def instance_ui_node_modules_exists(runner: Runner, target: RegressionTarget, *, remote: str | None) -> bool:
    """Whether the instance already has an npm-installed dependency tree.

    ``.package-lock.json`` is the marker npm itself writes inside
    ``node_modules`` once an install completes, so it distinguishes a finished
    tree from a directory left behind by an interrupted one.
    """
    result = runner.run(
        tenant_exec(target, "test -f ui/node_modules/.package-lock.json", remote=remote),
        check=False,
    )
    return result.returncode == 0


def normalize_runtime_config(runner: Runner, target: RegressionTarget, *, remote: str | None) -> None:
    config_path = f"{AVIBE_HOME}/config/config.json"
    script = textwrap.dedent(f"""
        import json
        from pathlib import Path

        path = Path({config_path!r})
        if not path.exists():
            raise SystemExit(0)
        payload = json.loads(path.read_text())
        ui = payload.setdefault("ui", {{}})
        changed = False
        if ui.get("setup_host") != {CONTAINER_UI_HOST!r}:
            ui["setup_host"] = {CONTAINER_UI_HOST!r}
            changed = True
        if ui.get("setup_port") != {target.ui_port!r}:
            ui["setup_port"] = {target.ui_port!r}
            changed = True
        if not changed:
            raise SystemExit(0)
        path.write_text(json.dumps(payload, indent=2))
    """).strip()
    runner.run(
        tenant_exec(
            target,
            f"{VENV_DIR}/bin/python scripts/prepare_regression.py --normalize-config {shlex.quote(config_path)} && "
            f"python3 - <<'PY'\n{script}\nPY",
            remote=remote,
        )
    )


def update_dependencies_and_build(
    runner: Runner,
    target: RegressionTarget,
    *,
    previous_fingerprints: dict,
    next_fingerprints: dict,
    force_deps: bool,
    build_ui: bool,
    force_ui: bool,
    remote: str | None,
) -> set[str]:
    """Bring the instance's artifacts up to date; report which ones it reconciled.

    The return value feeds ``reconciled_fingerprints``. A key belongs in it when
    the artifact on disk now corresponds to ``next_fingerprints[key]`` -- either
    because this run rebuilt it, or because the run skipped the rebuild
    precisely because the fingerprint already matched. A key is absent only when
    the run never looked, which is what ``--no-build-ui`` does to the UI.
    """
    runner.run(root_exec(target, f"python3 -m venv {shlex.quote(VENV_DIR)} || true", remote=remote))
    runner.run(root_exec(target, f"chown -R {SERVICE_USER}:{SERVICE_USER} {shlex.quote(VENV_DIR)}", remote=remote))
    python_changed = (
        force_deps
        or previous_fingerprints.get("python") != next_fingerprints.get("python")
        or not previous_fingerprints
    )
    if python_changed:
        runner.run(tenant_exec(target, f"{VENV_DIR}/bin/python -m pip install -U pip wheel", remote=remote))
    else:
        print("Python dependency fingerprint unchanged; skipping pip install.")
    needs_ui_dist = not instance_ui_dist_exists(runner, target, remote=remote)
    should_build_ui = build_ui or needs_ui_dist
    if needs_ui_dist and not build_ui:
        print("UI dist missing in synced source; building UI before editable install.")
    if should_build_ui:
        # A sync keeps ui/node_modules, so "the fingerprint did not change" only
        # licenses skipping npm ci when the tree it describes is actually there.
        needs_node_modules = not instance_ui_node_modules_exists(runner, target, remote=remote)
        ui_deps_changed = (
            force_ui
            or needs_node_modules
            or previous_fingerprints.get("ui_deps") != next_fingerprints.get("ui_deps")
            or not previous_fingerprints
        )
        if ui_deps_changed:
            runner.run(tenant_exec(target, "cd ui && npm ci", remote=remote))
        else:
            print("UI dependency fingerprint unchanged; skipping npm ci.")
        if (
            force_ui
            or needs_ui_dist
            or ui_deps_changed
            or previous_fingerprints.get("ui_source") != next_fingerprints.get("ui_source")
        ):
            runner.run(
                tenant_exec(
                    target,
                    "cd ui && npm run build",
                    remote=remote,
                )
            )
        else:
            print("UI source fingerprint unchanged; skipping npm run build.")
    if python_changed:
        runner.run(tenant_exec(target, f"{VENV_DIR}/bin/pip install -e .", remote=remote))
    # ``python`` is always reconciled: it either just installed, or it was
    # skipped because the previous fingerprint already equalled this one.
    reconciled = {"python"}
    if should_build_ui:
        reconciled |= {"ui_deps", "ui_source"}
    return reconciled


def restart_and_verify(runner: Runner, target: RegressionTarget, *, remote: str | None) -> None:
    runner.run(root_exec(target, "systemctl daemon-reload", remote=remote))
    runner.run(root_exec(target, f"systemctl enable --now {SERVICE_NAME}", remote=remote))
    runner.run(root_exec(target, f"systemctl restart {SERVICE_NAME}", remote=remote))
    runner.run(
        root_exec(
            target,
            (
                "for i in $(seq 1 60); do "
                f"systemctl is-active --quiet {SERVICE_NAME} && "
                f"curl -fsS http://127.0.0.1:{target.ui_port}/health >/dev/null && "
                f"curl -fsS http://127.0.0.1:{target.ui_port}/status | grep -q '\"state\":\"running\"' && "
                "exit 0; "
                "sleep 2; "
                f"done; journalctl -u {SERVICE_NAME} --no-pager -n 120; exit 1"
            ),
            remote=remote,
        )
    )


def prepare_show_runtime(runner: Runner, target: RegressionTarget, *, remote: str | None) -> None:
    source_result = runner.run(
        tenant_exec(target, 'printf "%s" "${VIBE_SHOW_RUNTIME_SOURCE:-}"', remote=remote),
        capture=True,
    )
    runtime_env = regression_show_runtime_env(source_result.stdout or "", SERVICE_HOME)
    runtime_env_prefix = " ".join(f"{key}={shlex.quote(value)}" for key, value in runtime_env.items())
    runtime_env_exports = "\n".join(f"export {key}={shlex.quote(value)}" for key, value in runtime_env.items())
    runner.run(
        tenant_exec(
            target,
            f"""
set -euo pipefail
{runtime_env_exports}
if [ "${{VIBE_SHOW_RUNTIME_SOURCE:-}}" = "archive" ]; then
  : "${{VIBE_SHOW_RUNTIME_ARCHIVE_PATH:?VIBE_SHOW_RUNTIME_ARCHIVE_PATH is required for archive runtime source}}"
  build_dir=$(mktemp -d)
  trap 'rm -rf "$build_dir"' EXIT
  git clone --depth 1 https://github.com/avibe-bot/vibe-show-runtime.git "$build_dir/runtime"
  (
    cd "$build_dir/runtime"
    npm ci
    npm run build
    npm run bundle:vibe-remote
  )
  set -- "$build_dir"/runtime/dist/vibe-show-runtime-node-*.tgz
  [ "$#" -eq 1 ] && [ -f "$1" ]
  install -D -m 0644 "$1" "$VIBE_SHOW_RUNTIME_ARCHIVE_PATH"
fi
""".strip(),
            remote=remote,
        ),
        timeout=env_int("REGRESSION_SHOW_RUNTIME_BUILD_TIMEOUT_SECONDS") or SHOW_RUNTIME_BUILD_TIMEOUT_SECONDS,
    )
    result = runner.run(
        tenant_exec(target, f"{runtime_env_prefix} {VENV_DIR}/bin/vibe runtime prepare --strict", remote=remote),
        check=False,
    )
    if result.returncode != 0:
        runner.run(tenant_exec(target, "rm -rf ~/.avibe/runtime/show-runtime/prebuilt/current", remote=remote), check=False)
        runner.run(tenant_exec(target, f"{runtime_env_prefix} {VENV_DIR}/bin/vibe runtime prepare --strict", remote=remote))
    runner.run(tenant_exec(target, f"{runtime_env_prefix} {VENV_DIR}/bin/vibe runtime status --json", remote=remote))


def cmd_doctor(args: argparse.Namespace) -> int:
    if not args.dry_run:
        require_incus()
    runner = Runner(dry_run=args.dry_run)
    checks = [
        ("version", incus("version")),
        ("daemon info", incus("info", *optional_remote_ref(args.remote))),
        ("projects", incus("project", "list", *optional_remote_ref(args.remote))),
        ("storage", incus("storage", "list", *optional_remote_ref(args.remote))),
        ("network", incus("network", "list", *optional_remote_ref(args.remote))),
    ]
    failed: list[str] = []
    for name, command in checks:
        result = runner.run(command, check=False)
        if result.returncode != 0:
            failed.append(name)
    if failed:
        print("Failed checks: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


def cmd_init_host(args: argparse.Namespace) -> int:
    if not args.dry_run:
        require_incus()
    runner = Runner(dry_run=args.dry_run)
    if args.minimal:
        if args.remote:
            raise RegressionError("init-host --minimal must run on the Incus host itself, not through a remote.")
        runner.run(incus("admin", "init", "--minimal"))
    return cmd_doctor(args)


def cmd_build_base(args: argparse.Namespace) -> int:
    if not args.dry_run:
        require_incus()
    runner = Runner(dry_run=args.dry_run)
    runner.run(incus("delete", remote_ref(args.remote, args.temp_instance), "--force"), check=False)
    runner.run(
        incus(
            "launch",
            remote_ref(args.remote, args.source_image) if args.remote and ":" not in args.source_image else args.source_image,
            remote_ref(args.remote, args.temp_instance),
            "--storage",
            args.storage_pool,
            "--network",
            args.network,
        )
    )
    runner.run(
        incus(
            "exec",
            remote_ref(args.remote, args.temp_instance),
            "--",
            "bash",
            "-lc",
            textwrap.dedent(
                """\
                set -euo pipefail
                apt-get update
                apt-get install -y bash ca-certificates curl git build-essential python3 python3-pip python3-venv rsync sudo tmux
                curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
                apt-get install -y nodejs
                # Install the agent backends under the service user's home so the
                # non-root avibe service can self-update them (`claude update`, etc.).
                # Root-global installs under /usr are not writable by the avibe user,
                # which is exactly what breaks self-update in regression. These land
                # root-owned at build time and are made avibe-owned per instance by the
                # `chown -R avibe:avibe /home/avibe` in cloud-init runcmd /
                # ensure_project_and_instance; the service PATH already prefers
                # /home/avibe/.local/bin over /usr.
                avibe_home=/home/avibe
                mkdir -p "$avibe_home/.local/bin" "$avibe_home/.npm-global"
                # Persist a user-writable npm prefix so claude-code/codex install here
                # AND future npm-based self-updates by the avibe user stay writable.
                printf 'prefix=%s/.npm-global\n' "$avibe_home" > "$avibe_home/.npmrc"
                HOME="$avibe_home" npm install -g @anthropic-ai/claude-code @openai/codex
                ln -sf "$avibe_home/.npm-global/bin/claude" "$avibe_home/.local/bin/claude"
                ln -sf "$avibe_home/.npm-global/bin/codex" "$avibe_home/.local/bin/codex"
                # OpenCode installs into the service user's home via its own updater.
                # HOME must be set on the piped `bash` (the installer), not on `curl`.
                curl -fsSL https://opencode.ai/install | HOME="$avibe_home" bash -s -- --no-modify-path
                if [ ! -x "$avibe_home/.opencode/bin/opencode" ]; then
                    echo "OpenCode installer did not produce an opencode binary" >&2
                    exit 1
                fi
                ln -sf "$avibe_home/.opencode/bin/opencode" "$avibe_home/.local/bin/opencode"
                # askill stays system-global: it is a bootstrap dependency, not a
                # self-updated agent backend.
                curl -fsSL https://askill.sh | sh -s -- -b /usr/local/bin
                export PATH="$avibe_home/.local/bin:$PATH"
                claude --version
                codex --version
                opencode --version
                askill --version
                node --version
                npm --version
                """
            ),
        )
    )
    runner.run(
        incus(
            "exec",
            remote_ref(args.remote, args.temp_instance),
            "--",
            "bash",
            "-lc",
            "cloud-init clean --logs || true",
        )
    )
    runner.run(incus("stop", remote_ref(args.remote, args.temp_instance), "--force"), check=False)
    runner.run(incus("image", "delete", remote_ref(args.remote, args.image)), check=False)
    publish_command = incus("publish", remote_ref(args.remote, args.temp_instance))
    if args.remote:
        publish_command.append(remote_ref(args.remote))
    publish_command.extend(["--alias", args.image])
    runner.run(publish_command)
    runner.run(incus("delete", remote_ref(args.remote, args.temp_instance), "--force"))
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    repo_root = current_repo_root()
    loaded_env_file = load_env_file(repo_root, args.env_file)
    if not args.dry_run:
        require_incus()
    metadata = WorktreeMetadata(repo_root, args.remote)
    # The lock comes before the row, because the lock is what says a run holds
    # this slug: `reconcile` prunes a reservation whose lock nobody holds, so a
    # row written outside it is a row that can be dropped while this run is still
    # building against it. Naming the lock needs only the environment's identity
    # -- the daemon that holds it and the project on it -- so the port is asked
    # for and recorded inside the lock that protects it --
    # picking a free port and reserving it stay one step under the mapping lock,
    # which is why the identity is observed here and not derived from a target.
    slug = target_slug(args, repo_root)
    lock_project = project_name_for(args.target, slug)
    # Built before the attempt, not inside it: releasing the reservation asks the
    # daemon what it holds, and a failure while acquiring the update lock would
    # otherwise reach that handler with no runner to ask through.
    runner = Runner(dry_run=args.dry_run)
    reservation: WorktreeReservation | None = None
    stopped_service_target: RegressionTarget | None = None
    try:
        with target_update_lock(repo_root, args.remote, lock_project, dry_run=args.dry_run):
            with metadata.locked(dry_run=args.dry_run):
                target = resolve_target(args, repo_root, dry_run=args.dry_run, slug=slug)
                reservation = metadata.reserve(target, dry_run=args.dry_run)
            target_exists = instance_exists(runner, args.remote, target.project, target.instance)
            if not args.dry_run and not target_exists and args.remote is None:
                # Reached only once the daemon has enumerated its instances and this one
                # was absent, so an occupied port is a real conflict with something else
                # rather than this environment's own proxy device.
                ensure_host_port_available(target.ui_host, target.host_port)
            seed_requires_env = not args.dry_run and (args.reset_mode != "none" or not target_exists)
            if seed_requires_env:
                require_runtime_seed_env()
            if target_exists:
                guard_paired_master_reset(
                    runner,
                    target,
                    reset_mode=args.reset_mode,
                    allow_reset_paired_master=getattr(args, "allow_reset_paired_master", False),
                    remote=args.remote,
                )
            ensure_project_and_instance(
                runner,
                target,
                image=args.image,
                storage_pool=args.storage_pool,
                network=args.network,
                cpus=args.cpus,
                memory=args.memory,
                disk=args.disk,
                processes=args.processes,
                remote=args.remote,
            )
            if not args.dry_run and not seed_requires_env and should_seed_state(runner, target, reset_mode=args.reset_mode, remote=args.remote):
                require_runtime_seed_env()
            stop_service_for_update(runner, target, remote=args.remote)
            stopped_service_target = target
            if seed_requires_env or loaded_env_file is not None or args.dry_run:
                write_runtime_env(runner, target, repo_root=repo_root, remote=args.remote)
            else:
                print("No regression env file loaded; preserving existing runtime env file.")
            migrate_legacy_backend_runtimes(runner, target, remote=args.remote)
            sync_source(runner, target, repo_root, remote=args.remote, clean=args.clean, include_ui_dist=args.no_build_ui)
            fingerprints = compute_fingerprints(repo_root)
            previous_fingerprints = read_existing_fingerprints(runner, target, remote=args.remote)
            invalidate_fingerprints(runner, target, remote=args.remote)
            reconciled = update_dependencies_and_build(
                runner,
                target,
                previous_fingerprints=previous_fingerprints,
                next_fingerprints=fingerprints,
                force_deps=args.force_deps,
                build_ui=not args.no_build_ui,
                force_ui=args.force_ui,
                remote=args.remote,
            )
            # ``prepare_show_runtime`` below is unconditional, so the run either
            # reconciles the show runtime or fails outright.
            reconciled = reconciled | {"show_runtime"}
            run_prepare_state(runner, target, reset_mode=args.reset_mode, remote=args.remote)
            normalize_runtime_config(runner, target, remote=args.remote)
            write_metadata(
                runner,
                target,
                repo_root,
                reconciled_fingerprints(previous_fingerprints, fingerprints, reconciled),
                remote=args.remote,
            )
            # Install updated runtime sources while the service is stopped so the
            # restarted process cannot keep serving code loaded before preparation.
            prepare_show_runtime(runner, target, remote=args.remote)
            restart_and_verify(runner, target, remote=args.remote)
            stopped_service_target = None
            reservation.complete()
    except BaseException:
        # Not `Exception`: Ctrl-C is how an `up` is abandoned in practice, and a
        # KeyboardInterrupt would otherwise leave exactly the row this exists for.
        # `None` covers failing before the row was written -- resolving a target,
        # or waiting on the update lock -- where there is nothing to give back.
        if stopped_service_target is not None:
            restart_service_after_failed_update(runner, stopped_service_target, remote=args.remote)
        if reservation is not None:
            reservation.release(runner)
        raise
    print_summary(target)
    return 0


def print_summary(target: RegressionTarget) -> None:
    print("")
    print("Incus regression environment is ready:")
    print(f"  URL: http://{target.ui_host}:{target.host_port}")
    print(f"  Target: {target.target}")
    print(f"  Project: {target.project}")
    print(f"  Instance: {target.instance}")
    print(f"  Show Runtime source: {regression_show_runtime_source()}")


def cmd_status(args: argparse.Namespace) -> int:
    repo_root = current_repo_root()
    load_env_file(repo_root, args.env_file)
    target = resolve_target(args, repo_root, dry_run=args.dry_run, allocate_port=False)
    if not args.dry_run:
        require_incus()
    runner = Runner(dry_run=args.dry_run)
    failed = 0
    for command in (
        incus("list", *optional_remote_ref(args.remote), project=target.project),
        root_exec(target, "avibe-regression-info && systemctl status avibe-regression --no-pager", remote=args.remote),
        tenant_exec(target, f"{VENV_DIR}/bin/vibe status", remote=args.remote),
    ):
        result = runner.run(command, check=False)
        failed += 1 if result.returncode != 0 else 0
    return 1 if failed else 0


def cmd_logs(args: argparse.Namespace) -> int:
    repo_root = current_repo_root()
    load_env_file(repo_root, args.env_file)
    target = resolve_target(args, repo_root, dry_run=args.dry_run, allocate_port=False)
    if not args.dry_run:
        require_incus()
    Runner(dry_run=args.dry_run).run(
        root_exec(target, f"journalctl -u {SERVICE_NAME} -f --no-pager", remote=args.remote),
        check=False,
    )
    return 0


def cmd_shell(args: argparse.Namespace) -> int:
    repo_root = current_repo_root()
    load_env_file(repo_root, args.env_file)
    target = resolve_target(args, repo_root, dry_run=args.dry_run, allocate_port=False)
    if not args.dry_run:
        require_incus()
    Runner(dry_run=args.dry_run).run(tenant_exec(target, "exec bash -l", remote=args.remote))
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    repo_root = current_repo_root()
    load_env_file(repo_root, args.env_file)
    target = resolve_target(args, repo_root, dry_run=args.dry_run, allocate_port=False)
    if not args.dry_run:
        require_incus()
    Runner(dry_run=args.dry_run).run(incus("stop", remote_ref(args.remote, target.instance), project=target.project), check=False)
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    repo_root = current_repo_root()
    load_env_file(repo_root, args.env_file)
    if args.target == MASTER_TARGET and not args.yes:
        raise RegressionError("Deleting the master regression environment requires --yes.")
    if not args.dry_run:
        require_incus()
    # Removing the objects and forgetting the row are one change to what this
    # slug names, so both happen under the environment's update lock -- the same
    # one `up` holds from before it writes its row until after it stamps it.
    # Without it, a delete landing while an `up` builds deletes nothing, because
    # the objects do not exist yet, drops that run's reservation anyway, and
    # leaves the finished environment with no row and its host port free for the
    # next slug: `complete` will not restore a row that is no longer the one its
    # run reserved. Naming the lock needs only the identity, as in `up`, so the
    # target is resolved inside it and nothing about this slug is read outside.
    slug = target_slug(args, repo_root)
    lock_project = project_name_for(args.target, slug)
    with target_update_lock(repo_root, args.remote, lock_project, dry_run=args.dry_run, blocking=False):
        target = resolve_target(args, repo_root, dry_run=args.dry_run, allocate_port=False, slug=slug)
        runner = Runner(dry_run=args.dry_run)
        runner.run(incus("delete", remote_ref(args.remote, target.instance), "--force", project=target.project), check=False)
        runner.run(incus("project", "delete", remote_ref(args.remote, target.project)), check=False)
        if target.target == WORKTREE_TARGET and not args.dry_run:
            metadata = WorktreeMetadata(repo_root, args.remote)
            metadata.forget([target.slug])
            if not metadata.owned:
                # The row describes a slug and host port on this machine, and this
                # deletion happened somewhere else, so `forget` above did nothing.
                # Saying so is all that is left to the caller: a slug used on both
                # daemons would otherwise lose its local port reservation the moment
                # the remote copy was removed, and lose it silently.
                print(f"Kept the local metadata for {target.slug}: it describes the local Incus daemon, not remote {args.remote}.")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Report what Incus holds, and drop metadata for environments it no longer has.

    This replaces the old `cleanup-stale`, which deleted an environment whose
    recorded worktree path had disappeared. That criterion could not work: the
    path records the checkout the runner was invoked from, so several
    environments created from one checkout share it, and it keeps existing after
    the worktree they were built for is gone. It therefore deleted running
    environments that were still wanted and kept ones nobody wanted.

    Nothing else about a regression environment says whether it is still wanted
    either -- a slug is chosen by the caller and an environment may sit on a
    detached HEAD with no branch to check for a merge -- so this command does not
    guess. It shows every environment with its state and provenance, leaves
    container deletion to an explicit `delete --slug`, and only ever removes
    metadata rows Incus has already outlived.

    "Already outlived" is deliberately strict: a row is dropped only when the
    daemon that owns it completed a listing, that listing held neither half of
    the environment, and no live run holds that slug -- the last of which is
    asked of the kernel rather than of the row, because a row cannot answer it
    (see `target_run_in_flight`). Every weaker reading of the same evidence would
    release a host port somebody else is using.
    """
    repo_root = current_repo_root()
    require_incus()
    # Enumeration must be a real listing even under --dry-run: `Runner.names`
    # answers [] for a dry run by contract, which would report every existing
    # environment as untracked metadata and offer to forget all of it. --dry-run
    # withholds the mapping write instead, which is this command's only change.
    runner = Runner(dry_run=False)
    metadata = WorktreeMetadata(repo_root, args.remote)
    authority = f"remote {args.remote}" if args.remote else "the local Incus daemon"
    remote_suffix = f" --remote {shlex.quote(args.remote)}" if args.remote else ""
    # The mapping is read, classified, and written under one lock -- taken through
    # the accessor, so it is held only when this file is what the decision is
    # about -- and no row can be added or completed midway through. That lock is
    # not what makes a reservation safe, though: `up` releases it as soon as the
    # row is written and keeps building for minutes afterwards, so a reconcile in
    # that window legitimately sees a row with no footprint yet. What protects
    # such a row is the target update lock its run holds across the whole window,
    # which is a live process rather than a record -- see `target_run_in_flight`.
    with metadata.locked(dry_run=False):
        environments = worktree_environments(runner, metadata)
        if not environments:
            print(f"No worktree regression environments exist in {authority}.")
            return 0

        live = [env for env in environments if env.exists]
        in_flight = [env for env in environments if not env.exists and env.in_flight]
        forgotten = [env for env in environments if not env.exists and not env.in_flight]

        if live:
            print(f"{len(live)} worktree regression environment(s) exist in {authority}:")
            for env in live:
                print(f"  {env.slug}  [{env.footprint}]  {describe_worktree_entry(env.entry)}")
            print()
            # Deletion stays explicit and per-environment. `delete` derives the
            # project and instance from the slug by naming convention, so it
            # reaches an environment with no metadata just as well as a tracked
            # one -- but only where that convention matches what Incus holds.
            #
            # The two lists overlap on purpose. An environment can hold a
            # convention-project instance and a stranded one at the same time, so
            # partitioning it into one bucket or the other has to be wrong about
            # something: either it offers a delete command that leaves an instance
            # running, or it withholds one that would reclaim most of the disk.
            # Saying both is the only honest report.
            deletable = [env for env in live if env.deletable_by_slug]
            if deletable:
                print("Delete any of them with:")
                for env in deletable:
                    print(
                        "  python3 scripts/incus_regression.py delete --target worktree "
                        f"--slug {shlex.quote(env.slug)} --yes{remote_suffix}"
                    )
            for env in live:
                if not env.reachable_by_slug:
                    # `delete --slug` validates its argument, so for this name it
                    # reaches nothing at all -- and the whole point of enumerating
                    # from Incus is to find environments the runner did not create,
                    # whose names nothing constrained. Printing the command anyway
                    # would advertise a reclamation that exits on its own argument,
                    # so the objects are named for a manual one instead.
                    observed = [env.project] if env.has_project else []
                    observed += sorted(f"{item.project}/{env.instance}" for item in env.instances)
                    print(
                        f"  {env.slug}: not a slug this runner accepts, so `delete --slug` would "
                        f"reject it. Reclaim by hand: {', '.join(observed)}."
                    )
                    continue
                for item in env.stranded_instances:
                    print(
                        f"  {env.slug}: instance lives in project {item.project}, not {env.project}. "
                        "Delete by slug would not reach it; reclaim it by hand."
                    )
            if not metadata.owned:
                # Said once rather than implied by every environment reading
                # "no runner metadata". `worktrees.json` records what this
                # machine's daemon holds, so annotating another daemon's
                # environments from it would attribute a local row's port,
                # branch, and commit to an environment that merely shares its
                # slug. One report, one authority; run `reconcile` with no
                # --remote for the local rows.
                print()
                print(f"Runner metadata is not shown: it describes the local Incus daemon, not {authority}.")

        if in_flight:
            if live:
                print()
            print(f"{len(in_flight)} metadata entr(ies) reserve a slug an `up` is still holding:")
            for env in in_flight:
                print(f"  {env.slug}  {describe_worktree_entry(env.entry)}")
            # No recovery to name here any more. This section is a live process
            # holding a lock, so an `up` that was killed is not in it: the kernel
            # dropped its lock as it died, and its row is reported below as an
            # environment this daemon no longer has, which `--yes` prunes.
            print("Left alone: that run holds this slug's update lock right now, and its port stays reserved.")

        if not forgotten:
            return 0
        if live or in_flight:
            print()
        # Only rows this daemon owns can reach here at all: a report about
        # another daemon annotates nothing from this file, so every slug it
        # lists came from that daemon's own projects and instances and is
        # therefore present. The "a remote never prunes" rule needs no branch
        # here any more; it is a consequence of who the rows belong to.
        print(f"{len(forgotten)} metadata entr(ies) describe environments {authority} no longer has:")
        for env in forgotten:
            print(f"  {env.slug}  {describe_worktree_entry(env.entry)}")
        # Reporting is what this command is for, so having found something to
        # report is not a failure: it exits 0 whether or not any row was stale.
        # Raising here made the documented plain `reconcile` exit non-zero for
        # exactly the case it exists to show, which reads as a broken command to
        # a `&&` chain, a CI step, or anyone who checks `$?` -- while the report
        # it just printed says the run went fine. `--yes` is what asks for the
        # write, and `--dry-run` withholds it even then.
        if args.dry_run or not args.yes:
            print("Nothing was changed. Dropping them and releasing their reserved host ports needs --yes and no --dry-run.")
            return 0
        metadata.forget(env.slug for env in forgotten)
        print(f"Dropped {len(forgotten)} stale metadata entr(ies). No instance was deleted.")
        return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Print commands without changing Incus.")
    # Keep --remote as an explicit escape hatch for the rare remote-ops case the
    # docs call out. Local dev defaults to None (no remote), and it is what names
    # the daemon every command acts on: `remote_ref` addresses it, and
    # `WorktreeMetadata` uses it to decide whether this machine's metadata is
    # evidence about that daemon at all. Both read the same value, which is why
    # it is normalized here -- the one place every command's --remote is defined
    # -- rather than at each reader.
    parser.add_argument("--remote", type=normalized_remote, help="Optional Incus remote name.")


def add_target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", choices=sorted(TARGETS), default=MASTER_TARGET)
    parser.add_argument("--slug", help="Explicit worktree slug for --target worktree.")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--host-port", type=int, help="Host port for the Web UI proxy.")
    parser.add_argument("--ui-host", help="Host/interface for the Incus UI proxy. Defaults to REGRESSION_PORT_BIND_HOST or 127.0.0.1 after env loading.")
    parser.add_argument("--ui-port", type=int, default=DEFAULT_UI_PORT)
    parser.add_argument("--worktree-port-start", type=int, default=DEFAULT_WORKTREE_PORT_START)
    parser.add_argument("--worktree-port-end", type=int, default=DEFAULT_WORKTREE_PORT_END)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check Incus readiness.")
    add_common(doctor)
    doctor.set_defaults(func=cmd_doctor)

    init_host = subparsers.add_parser("init-host", help="Optionally initialize Incus and check readiness.")
    init_host.add_argument("--minimal", action="store_true")
    add_common(init_host)
    init_host.set_defaults(func=cmd_init_host)

    build_base = subparsers.add_parser("build-base", help="Build/publish the reusable regression base image.")
    build_base.add_argument("--image", default=DEFAULT_IMAGE)
    build_base.add_argument("--source-image", default=DEFAULT_BASE_SOURCE_IMAGE)
    build_base.add_argument("--temp-instance", default="avibe-regression-base-build")
    build_base.add_argument("--storage-pool", default=DEFAULT_STORAGE_POOL)
    build_base.add_argument("--network", default=DEFAULT_NETWORK)
    add_common(build_base)
    build_base.set_defaults(func=cmd_build_base)

    up = subparsers.add_parser("up", help="Create/update a regression environment.")
    add_common(up)
    add_target_args(up)
    up.add_argument("--image", default=DEFAULT_IMAGE)
    up.add_argument("--storage-pool", default=DEFAULT_STORAGE_POOL)
    up.add_argument("--network", default=DEFAULT_NETWORK)
    up.add_argument("--cpus", default="4")
    up.add_argument("--memory", default="8GiB")
    up.add_argument("--disk", default="80GiB")
    up.add_argument("--processes", default="8192")
    up.add_argument("--reset-mode", choices=["none", "config", "all"], default="none")
    up.add_argument(
        "--allow-reset-paired-master",
        action="store_true",
        help="Allow reset-mode config/all to delete Avibe Cloud pairing state from the master regression environment.",
    )
    up.add_argument("--clean", action="store_true", help="Wipe the synced source completely, including the UI dependency tree and build output a sync normally keeps.")
    up.add_argument("--force-deps", action="store_true", help="Force Python dependency refresh.")
    up.add_argument("--no-build-ui", action="store_true", help="Skip npm ci/build for UI assets.")
    up.add_argument("--force-ui", action="store_true", help="Force npm ci and npm run build even when the UI fingerprint is unchanged.")
    up.set_defaults(func=cmd_up)

    for name, func in (
        ("status", cmd_status),
        ("logs", cmd_logs),
        ("shell", cmd_shell),
        ("down", cmd_down),
        ("delete", cmd_delete),
    ):
        sub = subparsers.add_parser(name)
        add_common(sub)
        add_target_args(sub)
        if name == "delete":
            sub.add_argument("--yes", action="store_true")
        sub.set_defaults(func=func)

    reconcile = subparsers.add_parser(
        "reconcile",
        help="List worktree environments Incus holds, and forget metadata for ones it no longer has.",
    )
    add_common(reconcile)
    reconcile.add_argument("--yes", action="store_true")
    reconcile.set_defaults(func=cmd_reconcile)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RegressionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        # A failing `check=True` step is an expected outcome here -- a busy host
        # port, a device Incus refuses, a daemon that went away mid-run. Report
        # which command failed instead of unwinding a traceback over it.
        print(f"Command failed with exit code {exc.returncode}: {shlex.join(exc.cmd)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
