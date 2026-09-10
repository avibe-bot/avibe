# Inactive CLIProxyAPI native-intent candidate

This directory is the maintained source home for a **local, inactive patch**.
It does not change Avibe's manifest, installed engine, configuration, release
guard, or runtime lifecycle. A passing test is not a shipped engine repair.

Base: `router-for-me/CLIProxyAPI` `v7.2.149`,
`2a6b87aca083a5bf498ac1f68a1b636c500d7aaa`. Do not substitute upstream HEAD.
The patch includes its Go policy and consuming tests; the temporary engine
checkout is not the maintained source of truth. See the English assessment in
`docs/plans/model-hub-engine-intent-policy-assessment.md`.

## Contract

- Native Messages, Chat and verified Responses representations keep their
  prepared reasoning fields without a suffix, independent of catalog metadata.
- Native Kimi objects carried by Chat are recognized from original
  `thinking.type`/`thinking.effort`; only the legacy `reasoning_effort` alias is
  removed. Keep-only/legacy-only input still uses conversion. Suffix wins.
- Explicit disable precedes capability gating and summary activation.
  Missing metadata uses source-aware capability-free conversion; known positive
  conversion constraints remain. No fake `UserDefined` or capability metadata.
- xAI's duplicate catalog-based final-egress deletion is removed; its actual
  protocol sanitization, payload rules, replay and tools remain.
- All Source, prefix, alias, model, exact credential snapshot, OAuth and
  replacement ownership remains unchanged, including the existing Chat hint.

## Apply and verify

After separate execution acceptance, use an explicitly authorized, already-running
native AArch64 little-endian 64-bit Linux development guest with Git, Python 3.12,
util-linux (`unshare`, `setpriv`), iproute2 and
transient sudo. This recipe does not provision a guest or change packages,
services, routes or Incus resources. Its macOS `sandbox-exec` path now denies
all egress and is only for pure-source diagnostics. Executing the pinned
Linux arm64 Go toolchain, including compilation, requires the Linux envelope;
there is no macOS engine-build or network-suite fallback.

Current acceptance boundary: **locally qualified inactive source; remote
delivery and OS execution remain held**. The sixth findings-bearing head,
`90cfc9b3dc99d54607561eb91783c69690c4888e`, received terminal review
`5160886376` with two additional findings: macOS service IPC and multiply-linked
source files. The full circuit has eighteen originals across six reviewed
heads: twelve resolved and six unresolved, including the preceding UID-keyring,
Linux IPC, privileged child-custody and complete-home findings. Successful
seventeen-job CI, CLEAN merge state and historical inactive-source acceptance
do not override those findings. The orchestrator independently inspected and
accepted the bounded correction as inactive source. Before this final prose
update, the lane passed 1,001 focused cases and 1,457 complete pure cases;
the orchestrator independently passed the same 1,457-case suite. Lane Ruff
0.4.9, all fifteen Python ASTs, nine shell and six embedded Python syntax
checks, and precisely scoped whitespace checks also passed on those bytes.
Final-prose qualification and any conditional local-only commit require their
own records; neither is predeclared here. Review-thread closure remains held.

The macOS helper now constructs default-deny policy. Only process-exec and
process-fork are positive non-file operations; no Mach/XPC, POSIX/System V IPC
or other service exceptions are granted. Finite file grants and explicit
network/file denies remain; process authority does not grant a file path.
The existing regular-file owner now binds reads to a no-follow, nonblocking
single-link descriptor and rejects observed pathname/descriptor or metadata
drift at admission and completion. Source digests and actual input/candidate
reads reuse it; apply checks the complete source tree before patch writes.
Unchanged single-link trees retain the same digest encoding. These checks
reject preexisting hardlink aliases, not all future concurrent changes:
trusted preparation/content custody throughout the diagnostic interval and
the intended read-only mount boundary remain separate prerequisites, not an
atomic snapshot guarantee.

The candidate extends the existing namespace/storage owners, not the engine
intent patch. Its Linux libc mount/proc-FD/overmount behavior, native ABI and
seccomp installation/inheritance/keyring denial have NOT been executed or
accepted. IPC separation and end-to-end privileged closure still need separate
Linux evidence. No source-only test can establish those kernel properties.
The macOS finite input policy's supported interpreter/system startup closure
and positive/negative enforcement also remain UNMET. Do not execute the
commands below on these bytes without the orchestrator's later release.

The preceding fifth candidate, including the corrected synthetic ownership
fixture, passed 335 focused HOST cases. Its pre-final-document input view
passed the orchestrator's 1,420-case pure run. Final exact90 inputs separately
passed lane/root 1,420-case runs and Ruff/static/whitespace qualification.
Those results remain historical, not validation of the sixth correction.
The proven HOST route used a hash-admitted, readonly 23-file view outside real homes,
preserving relative layout, with files 0444 and directories 0555. A separate
task-writable execution subtree, fresh task storage and an independent outer
HOST policy denied network, production writes and all real-home reads/metadata.
No home-input exception or installed-state change was needed.

The lane's earlier complete run remains failed: 1,071 passed before documented
preparation hit denied home-ancestor resolution. The earlier focused failure
remains 120 passed/one failed on inconsistent synthetic GID metadata; the
original collection failure exited 2 with zero tests. None is relabeled by
the later input-layout or fixture correction. Pure passes establish only
intercepted source consumers; exact90's inactive-source acceptance never
established OS enforcement or acceptance of these new omissions. The emitted-program
case contains 5,140 internal decode assertions in ONE test, not result rows
or Linux execution.

Historical fourth-round source `10b1` passed 1,190 pure cases, pinned Ruff 0.4.9
and syntax/whitespace gates, including independent root execution. Historical
scratch mechanism proof passed 51 HOST tests and executed 5,140 assertions
decoding its actual 88-byte program; its stdout records a digest/count and
51-test summary, not 5,140 individual result rows. Neither validates these new
maintained bytes. Preserve both failed macOS attempts: the lane's nested route
returned inner 71 (`sandbox_apply: Operation not permitted`) and outer 1;
the root's later single-profile child returned -6 with empty streams and
outer 1. No child access assertion passed. The root's earlier incorrect 0755
assumption was corrected to the unchanged trusted Python's actual root-owned
0775 mode; no installed permission was changed. No new macOS proof is claimed.

Historical third-round boundary: the third reviewed head exposed configured user
storage missing from pre-write protection. The orchestrator diagnosed the
repeated isolation class and authorized only a bounded recipe/test/doc correction
and pure validation. Independent inspection of the first 733-consumer correction
then reproduced a remaining README preparation gap: source/state child aliases
were not admitted before Git initialization or mkdir. The entry below now checks
the complete fresh preparation plan before any write. The corrected local recipe
passes 1,076 maintained pure consumers, pinned Ruff 0.4.9 and
document/AST/whitespace gates. That exact snapshot subsequently received one
bounded direct-sudo/context diagnostic and independent native original readback,
under the assessment's supplementary source criterion. Its OLD Watch raw outer
streams remain unavailable and complete original transport accounting is UNMET.
No new privileged probes or Go/build/wire execution are released. The preceding
second-round recipe genuinely passed 361 pure consumers and separate lane and
independent orchestrator Linux test/build/wire runs. Those full source results
remain acceptance of their exact earlier recipe, not these changed admission
and caller-context paths. The Go patch and frozen inputs have not changed.
The earlier watcher-only failure and all historical artifacts remain preserved.
The still-earlier macOS wildcard-loopback runs are not isolation acceptance.
No source tests or Avibe PR merge ship an engine repair.

Set `engine_task` to an explicitly allocated, canonical, dedicated child of
`/tmp` or `/var/tmp`, never either broad root. The privileged CLI rejects home
layouts and aliases into `/home`, `/root` or `/Users` before setup.
All writable-root consumers share one original-user storage model: passwd and
effective HOME, fixed `.avibe`, `.vibe_remote`, `.codex`, `.claude` children,
XDG config/cache/data/state/runtime, and AVIBE_HOME/CODEX_HOME/CLAUDE_CONFIG_DIR.
Unset or empty overrides use defaults; XDG defaults are `.config`, `.cache`,
`.local/share` and `.local/state` beneath effective HOME, with no runtime
fallback. Relative, parent-traversing or malformed configured paths fail closed
without printing their values. Home/broad roots and their ancestors remain
forbidden; storage roots reject equality, ancestors, descendants and lexical
or canonical aliases, including writable ancestors containing protected targets.
The macOS diagnostic also excludes complete original passwd/effective homes
and their lexical/canonical aliases, not merely product/configuration children.
`sandbox_prefix(task, required_inputs=(...))` requires a finite explicit set of
staged recipe/source/interpreter/system inputs outside those roots. Every
grant, including sandbox-exec, `/dev/null` and system/tool inputs, goes through
the same storage admission. Whole home/project/task ancestors and broad
`/usr`, `/System` or `/Library` grants refuse. The default-deny policy permits
only finite admitted reads, task writes and the two named process operations,
denies network, and has no global
read/metadata or unsandboxed fallback. Policy-construction tests are not
sandbox enforcement or inherited-port confinement evidence; the startup
input/operation closure remains an execution
gate, not a reason to widen access.

Begin from the invoking user's original environment, not `env -i`, a task HOME,
or an already-sanitized sudo shell. Set `recipe_source` to this inspected
maintained directory. The allocated task must be empty, caller-owned and mode
0700. Run this entire preparation entry: it admits the complete finite write
plan using the original context before any mkdir, copy, Git initialization or
export. It refuses existing contents, including empty children, links and
prior evidence; preserve them and obtain a separately allocated fresh task.
Do not run the later steps if this entry fails, or share/mutate its destinations
with another process while preparing them. Every pre-envelope Python command
below selects the already trusted `/usr/bin/python3` explicitly and uses `-I -B`.
Isolation ignores cwd, PYTHONPATH, user site and Python startup configuration;
it does not replace the original HOME/XDG/product variables used by admission.
Each command explicitly selects the inspected canonical recipe import directory.
The installed Python and its standard library are trusted prerequisites.

```sh
if ! /usr/bin/python3 -I -B - "$recipe_source" "$engine_task" <<'PY'
import shutil, sys
from pathlib import Path
recipe = Path(sys.argv[1])
if not recipe.is_absolute() or recipe.resolve(strict=True) != recipe or not recipe.is_dir():
    raise ValueError("Select the inspected canonical recipe directory.")
sys.path.insert(0, sys.argv[1])
from isolation import preparation_directories, safe_git
task = Path(sys.argv[2])
directories = preparation_directories(task)
for directory in directories:
    if directory not in (task / "recipe", task / "source"):
        directory.mkdir(mode=0o700)
shutil.copytree(sys.argv[1], task / "recipe",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "._*", ".DS_Store"))
safe_git(task / "source", "init")
PY
then
  exit 1
fi
```

The same plan covers `recipe`, `source`, `state`, `downloads`, `toolchain`,
`uv-toolchain`, `venv` and `fixtures`, all generated environment/shared Go cache
children from `environment_directories`, and the setup-only `state/uv-cache`.
All children start fresh, so download/extraction/venv internals cannot reuse
preexisting aliases. Never redirect a later setup destination outside this
plan or reuse the entry to reset an existing task. Do not use a shared checkout.
Fetching prerequisites remains a separate authorization, not part of admission:

```sh
/usr/bin/python3 -I -B - "$engine_task" <<'PY'
import sys
from pathlib import Path
task = Path(sys.argv[1])
recipe = task / "recipe"
if not recipe.is_absolute() or recipe.resolve(strict=True) != recipe or not recipe.is_dir():
    raise ValueError("Select the inspected canonical recipe directory.")
sys.path.insert(0, str(recipe))
from isolation import safe_git
safe_git(task / "source", "fetch", "--depth=1",
         "https://github.com/router-for-me/CLIProxyAPI.git",
         "2a6b87aca083a5bf498ac1f68a1b636c500d7aaa")
safe_git(task / "source", "checkout", "--detach", "FETCH_HEAD")
PY
```

`safe_git` is the same command-local boundary used by fixture export and
verify/apply. It selects trusted `/usr/bin/git`, an empty init template, no
system/global/environment configuration, no pager/credential helper, and only
HTTPS transport. No installed configuration is changed. Local config is read
without includes and must contain only ordinary core bookkeeping, user name/
email, remote URL/fetch and branch remote/merge declarations. Other local
settings (including includes, extensions, hooks, filters, fsmonitor, redirects
and external diff commands) refuse before an operational command. Diff also
disables external/textconv execution. Preserve refused repositories unchanged.
An unusual local configuration needs independent inspection, not a reset.

Only separately authorized prerequisite preparation may use public networking. `prerequisites.json`
records exact Linux arm64 Go 1.26.4 and uv 0.9.8 archive URLs and SHA256 values.
Download only into the admitted `"$engine_task/downloads"` (Go archive
`go.tar.gz`, uv archive `uv.tar.gz`), verify hashes before extraction, and
extract Go into `"$engine_task/toolchain"` and uv into
`"$engine_task/uv-toolchain"`. Do not install system packages or use a moving
toolchain. Verify the extracted compiler, tools and runtime files against the
SHA256-verified archive before using Go, not merely its version string:

```sh
/usr/bin/python3 -I -B - "$engine_task" <<'PY'
import json, sys
from pathlib import Path
task = Path(sys.argv[1])
recipe = task / "recipe"
if not recipe.is_absolute() or recipe.resolve(strict=True) != recipe or not recipe.is_dir():
    raise ValueError("Select the inspected canonical recipe directory.")
sys.path.insert(0, str(recipe))
from execution_inputs import verify_go
pin = json.loads((task / "recipe/prerequisites.json").read_text())["linux_arm64_go"]
print(json.dumps(verify_go(task / "toolchain", task / "downloads/go.tar.gz",
                          task / "toolchain/bin/go", pin), indent=2))
PY
```

The same verifier is mandatory inside every test/build/wire phase. It rejects
missing, extra or modified files, changed executable modes, escaping links,
hard-link aliases, wrong architecture and selection of another compiler.
The normalized archive/tree identity includes all compiler tools and runtime
sources, not just `bin/go`. Download locked Go modules using that Go; no catalog refresh or
provider account is involved:

```sh
env -i PATH="$engine_task/toolchain/bin:/usr/bin:/bin" \
  HOME="$engine_task/state/home" \
  TMPDIR="$engine_task/state/tmp" GOENV=off GOTOOLCHAIN=local \
  GOPATH="$engine_task/state/go" GOMODCACHE="$engine_task/state/mod" \
  GOCACHE="$engine_task/state/cache" GOMAXPROCS=2 GOMEMLIMIT=1536MiB \
  go -C "$engine_task/source" mod download
```

Export the complete tracked Avibe fixture. Set `avibe_checkout` to a repository
containing the declared base; its dirty, staged and untracked files are ignored.
The exporter allocates an exclusive child of the admitted `fixtures` directory;
retain that actual path as `fixture_root`. Any preparation on another machine
requires its own complete original-caller admission, not this task's authority:

```sh
fixture_root=$(/usr/bin/python3 -I -B - "$avibe_checkout" "$engine_task" <<'PY'
import json, sys
from pathlib import Path
recipe = Path(sys.argv[2]) / "recipe"
if not recipe.is_absolute() or recipe.resolve(strict=True) != recipe or not recipe.is_dir():
    raise ValueError("Select the inspected canonical recipe directory.")
sys.path.insert(0, str(recipe))
from fixture import export_fixture
root, identity = export_fixture(
    Path(sys.argv[1]), Path(sys.argv[2]) / "fixtures",
    json.loads((recipe / "inputs.json").read_text()),
)
print(root)
PY
) || exit 1
```

The exporter checks exact commit tree, archive and complete path-independent
source digest. Actual config writer/state/mock imports use only this export,
never current-worktree `sys.path`. Build the venv from its frozen `uv.lock`,
without installing Avibe or running a backend. Set `uv_binary` to the uv
executable extracted during the separately verified setup step:

```sh
env -i PATH=/usr/bin:/bin HOME="$engine_task/state/home" \
  XDG_CONFIG_HOME="$engine_task/state/config" \
  UV_CACHE_DIR="$engine_task/state/uv-cache" \
  UV_PROJECT_ENVIRONMENT="$engine_task/venv" UV_LINK_MODE=copy \
  "$uv_binary" --no-config sync --project "$fixture_root" --frozen \
  --no-install-project --no-editable --no-managed-python --python /usr/bin/python3
```

Apply once to a clean exact-base checkout. Reapplying to a dirty checkout is
refused; it never resets, stashes, or overwrites someone else's changes:

```sh
/usr/bin/python3 -I -B -c \
  'import sys; from pathlib import Path; p=Path(sys.argv.pop(1)); assert p.is_absolute() and p.resolve(strict=True)==p and p.is_dir(); sys.path.insert(0,str(p)); import verify; verify.main()' \
  "$engine_task/recipe" apply \
  --source "$engine_task/source" --state "$engine_task/state"
```

The namespace envelope is mandatory. Inspect its exact source, then run
`/bin/true` in `none` and `loopback` modes plus `/bin/false` to confirm failed
receipt/cleanup behavior. Also use the bounded `timeout` phase with a harmless
`/bin/sh -c 'sleep 30 & wait'` to check PID-namespace teardown after the
one-second candidate deadline. The nonzero/timeout terminal receipts must
remain **failed**, even when those diagnostic expectations pass.
Use new receipt names every time; collisions fail
closed and preserve prior evidence. Example:

```sh
caller_storage_sha256=$(/usr/bin/python3 -I -B - "$engine_task/recipe" <<'PY'
import sys
from pathlib import Path
recipe = Path(sys.argv[1])
if not recipe.is_absolute() or recipe.resolve(strict=True) != recipe or not recipe.is_dir():
    raise ValueError("Select the inspected canonical recipe directory.")
sys.path.insert(0, sys.argv[1])
from isolation import storage_context
print(storage_context().fingerprint())
PY
) || exit 1
/usr/bin/sudo -n /usr/bin/python3 -I -B -c \
  'import sys; from pathlib import Path; p=Path(sys.argv.pop(1)); assert p.is_absolute() and p.resolve(strict=True)==p and p.is_dir(); sys.path.insert(0,str(p)); import namespace; namespace.main()' \
  "$engine_task/recipe" \
  --root "$engine_task" --source "$engine_task/source" \
  --fixture "$fixture_root" --state "$engine_task/state" \
  --recipe "$engine_task/recipe" --toolchain "$engine_task/toolchain" \
  --python-env "$engine_task/venv" \
  --go-archive "$engine_task/downloads/go.tar.gz" --phase probe --network none \
  --receipt probe-none-unique.json --caller-storage-sha256 "$caller_storage_sha256" \
  -- /bin/true
```

After independent probe acceptance, the following narrow helper runs the
maintained phases with the same mounted inputs. Build uses `none`; test/wire
use the private loopback. The small caller imports the same derived budget
as the parent and verifier; a managed Watch must have timeout/lifetime 0
and must not impose a shorter total timeout. Run phases sequentially:

```sh
run_phase() {
  /usr/bin/python3 -I -B - \
    "$engine_task" "$fixture_root" "$1" "$2" "$3" "${4-}" <<'PY'
import subprocess, sys
from pathlib import Path
task, fixture = Path(sys.argv[1]), Path(sys.argv[2])
phase, network, receipt, build = sys.argv[3:]
recipe = task / "recipe"
if not recipe.is_absolute() or recipe.resolve(strict=True) != recipe or not recipe.is_dir():
    raise ValueError("Select the inspected canonical recipe directory.")
sys.path.insert(0, str(recipe))
from budgets import PHASES
from isolation import storage_context
storage = storage_context()
storage.validate(task)
entry = ("import sys; from pathlib import Path; p=Path(sys.argv.pop(1)); "
         "assert p.is_absolute() and p.resolve(strict=True)==p and p.is_dir(); "
         "sys.path.insert(0,str(p)); import namespace; namespace.main()")
command = ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-B", "-c", entry, str(recipe)]
paths = {"root": task, "source": task / "source", "fixture": fixture,
         "state": task / "state", "recipe": recipe, "toolchain": task / "toolchain",
         "python-env": task / "venv", "go-archive": task / "downloads/go.tar.gz"}
for name, path in paths.items():
    command += ["--" + name, str(path)]
selection = ["--build", build] if build else []
command += ["--phase", phase, "--network", network, "--receipt", receipt,
            "--caller-storage-sha256", storage.fingerprint(), *selection, "--",
            str(task / "venv/bin/python"), "-B", str(recipe / "verify.py"), phase,
            "--source", str(task / "source"), "--state", str(task / "state"),
            "--fixture", str(fixture), *selection]
subprocess.run(command, check=True, close_fds=True, timeout=PHASES[phase].driver_seconds)
PY
}
run_phase test loopback test-unique.json &&
run_phase build none build-unique.json &&
run_phase wire loopback wire-unique.json "$engine_task/runs/build-unique.json"
```

`budgets.py` is the single deadline owner: test permits three sequential
600-second commands, plus input/preflight/setup/cleanup allowances. Its
candidate, namespace and caller limits are 1980, 2100 and 2130 seconds.
Build/wire have one 600-second command and limits 780/900/930 seconds.
The parent also checks that the phase uses its required network mode before
setup. A timeout or early failure retains evidence and never starts a later
phase automatically.

The public parent requires the original caller's storage digest and independently
reconstructs it from its live `/usr/bin/sudo` parent's Linux proc exec-time
environment and the invoking UID's passwd identity. Only selected storage
variables are retained; no raw environment or protected paths are printed.
The proc reads are bounded, anchored and checked for process continuity.
Root's sanitized HOME is never substituted for the user's original context.
A missing/mismatched digest, non-sudo parent, unavailable/scrubbed environment
or changed process fails before any receipt, run, listener, subprocess or mount.
This deliberately supports the documented direct sudo invocation; another sudo
process layout is not an excuse for a fallback. Deliberately discarded variables
cannot be recovered: rerunning from a sanitized shell is not supported.

Before the first privileged allocation, the parent opens the canonical task
root without following a link and admits its actual directory type, caller UID
and exact0700 mode. That one ExitStack-owned descriptor is reused for receipts
and runs, with custody/name-identity rechecked at each consumer. A missing,
replaced, unreadable, foreign or wrong-mode root fails closed; the parent never
repairs its ownership or permissions. Rootfs is an exclusive child of the
root-owned receipts/control descriptor, not the caller-writable task directory.
Cleanup removes only its original empty identity relative to that retained
protected parent; replacement trees and prior evidence remain untouched.

The parent pins source, fixture, recipe, toolchain, Python environment, regular
Go archive, state, allocated output and selected build through component-relative
no-follow/nonblocking/close-on-exec handles. Selected build custody additionally
matches the exact output identity recorded in its prior parent-owned receipt.
Installed system trees retain explicit trusted-system prerequisites; the four
allowed character devices use Linux O_PATH/no-follow with exact device type/
major/minor. Directory pins preserve identities, not mutable file contents.
Existing fixture/toolchain/content checks remain necessary.

The fixed stdlib-only trampoline authenticates its bounded unnamed parent
handoff before recipe imports. Only budgets/isolation/namespace module bytes
are pread from pinned regular files, checked against parent-inspected digests
and compiled; there is no privileged pathname import or cwd bootstrap.
The initial inspected interpreter, standard library, modules and system inputs
remain trust prerequisites. `close_fds`/`pass_fds` carry the complete actual
handle set through unshare; malformed handoff/import/setup paths close partial
child acquisitions.

The direct typed libc mount adapter uses retained source FDs and protected
target-parent FD/component pairs, never mount(8) helpers. It reopens the
protected rootfs child AFTER tmpfs mounting to obtain the overmount view.
All target topology, metadata and marker handles are prepared before binding
caller-writable contents. Remounts use protected parent/component targets,
not old underlying placeholder FDs. After marker completion and readonly
rootfs remount, fchdir(new-view), chroot(".") and chdir("/") precede closure of
all host/setup/input handles. These are source contracts; proc-FD binds,
overmount views and installed-kernel behavior remain unexecuted premises.

Root adds its own passwd-home protection, then carries the stable context only
through the existing unnamed parent control and root-owned private proof.
Child consumers reuse it only after verifying all four namespaces (mnt/net/pid/ipc), dropped
privilege, context digest and exclusive invocation/output identity. Generated
task HOME/XDG/cache does not redefine production. No public skip/allow flag,
environment marker or caller-supplied safe-root dictionary grants an exception.
Public preflight/terminal evidence contains only the storage-context digest;
the private root-owned proof retains the original locations.

The private network has only its own loopback: dynamic Go `httptest`, engine
and replacement mock listeners share it, while guest/host listeners and
external networks are unreachable. Mount propagation becomes private before
any bind. Source, fixture, recipe and toolchain are read-only; task state is
writable. Guest/user homes and namespace handles are hidden. Child code runs
without capabilities or supplementary groups, with no-new-privileges and no
inherited supervisor descriptors. PID 1 exit destroys remaining descendants.
There are no public internal re-entry flags: the public CLI always invokes
util-linux `unshare`. A fixed isolated-Python trampoline consumes an unlinked
root-owned control descriptor, closes it, and compares all four actual
kernel namespace identities with the parent-captured identities before the
first mount or interface operation. Caller-supplied dictionaries cannot select
the private path. This protects the approved CLI, not arbitrary root Python.

The candidate's finite keyring boundary is the inspected 11-instruction,
88-byte classic-BPF program with SHA256
`196f5affb55a9d30299561255adb1027f9a985aba9762b9c66624ed5cded604d`.
It checks native `AUDIT_ARCH_AARCH64=0xc00000b7`, kills foreign architectures
and unsigned syscall numbers at or above `0x40000000`, returns EPERM for
add_key/request_key/keyctl (217/218/219), and permits other native low numbers.
It is not a general kernel sandbox or an anonymous-session-keyring claim.
The typed ctypes/prctl adapter requires NNP installation, filter installation,
NNP=1 and filter-mode=2 readback, in order. Only successful installation adds
the exact ABI/program identity to the immutable marker, before both dropped
preflight and candidate. Live consumers require four complete distinct
namespace IDs, zero capabilities, NNP=1, Seccomp=2 AND the trusted exact marker
binding; Seccomp=2 alone does not identify a filter. Real fork/exec inheritance,
harmless task-owned-key denial and IPC isolation remain future execution gates.

Each parent receipt name owns exactly one fresh `runs/<receipt-name>/`
directory for the invocation's logs, HOME/temp, binary and candidate records.
Only that output is writable; the parent-owned `runs` collection and other
runs are not mounted. Shared task caches remain separate under `state`.
Wire must explicitly select a prior successful build output, which is mounted
read-only. It verifies that build's identity, receipt, binary and complete
artifact-tree digest and binds the selection into its own `wire.json`.
There is no fixed `build.json`, binary or hidden latest pointer in shared
state. Failures, output collisions and later invocations preserve earlier
files; the recipe never deletes a run directory.

Parent receipts live under `"$engine_task/receipts"`, outside child mounts.
They are exclusively opened before execution. Original preflight travels
through an unlinked supervisor descriptor closed before candidate launch;
mutable child-state artifacts and candidate stdout cannot replace it. Keep
parent receipts and before/after input/lifecycle evidence outside candidate
state. Both unrelated outside IPv4/IPv6 sentinels must observe zero connections.
A failed preflight, command or cleanup is never a passing receipt.

The parent terminal receipt is still the sole trusted preflight/cleanup
record. Candidate `build.json`/`wire.json` and logs are supplementary artifacts,
not privileged attestations. Parent build selection reads only its own
no-follow, regular-file terminal receipt before execution; it never reads or
chowns child-controlled artifact paths after execution.

### Execution-input inventory

| Input | Owner and actual evidence |
| --- | --- |
| Engine base/patch/catalogs/`go.sum` | `verify_inputs` and `verify_candidate` require the exact base, complete changed-file inventory and fixed bytes; the complete source digest is also bound before/after. No source refresh. |
| Go archive/extraction/selected compiler | `execution_inputs.verify_go` verifies the archive SHA and normalized full extracted tree before any Go invocation, enforces Linux arm64 and the selected executable, then repeats verification after commands. Raw `go version` is separately captured; a version string is not provenance. |
| Frozen Avibe fixture | Complete tracked commit export, tree/archive/source hashes, checked before/after. The actual writer/state/mock imports use only this readonly export. |
| Recipe | Complete tree digest includes Python, declarations, patch, tests and documentation, rather than claiming identity for an unmeasured script subset. |
| Python/venv/lock | Existing guest Python 3.12 and frozen-lock venv setup are a trusted prerequisite. The selected venv/interpreter, complete venv tree and frozen `uv.lock`/`pyproject.toml` digests are measured before/after. This is **not** archive attestation of Python or independent proof that every installed distribution matches the lock. |
| Guest OS/Python/util-linux | Existing authorized guest trusted base, not newly pinned or provisioned by this recipe. Namespace, privilege, mount and sentinel facts are measured by the parent/probe. |
| uv | Setup-only declared archive/version. Preparation verifies its archive manually before extraction; execution phases do not invoke uv or claim to attest its extraction. |
| Module/build caches | Shared task state prepared through Go's locked-module workflow, not a new cryptographically attested dependency distribution. `go.sum`, readonly module mode and offline operation remain enforced; cache contents are not promoted to frozen source or a production reproducibility claim. |

The test/build/wire input records bind every verified or measured field above
with its stated trust category. Source, Go, fixture, recipe and measured venv
are rechecked even after a command fails. Wire refuses stale or changed
selected-build artifacts. Tests/builds use `GOPROXY=off`, checksum
verification, `-mod=readonly`, `GOMAXPROCS=2`, `-p=1`, isolated HOME/XDG/cache,
and private-network OS isolation. The Go memory setting is a soft limit, not an
OS memory cap.

The maintained `test` phase runs:

```text
go test -mod=readonly -p=1 -count=1 ./internal/thinking/... ./internal/modelconfig ./internal/runtime/executor/helps ./sdk/cliproxy/auth
go test -mod=readonly -p=1 -count=1 -run '^TestThinking|^TestSummaryIntent' ./test
go test -mod=readonly -p=1 -count=1 ./internal/runtime/executor
```

The `build` phase runs `go build -mod=readonly -p=1 -trimpath
-buildvcs=false ... ./cmd/server` with `CGO_ENABLED=0`. This is a diagnostic
host binary, not a production release artifact or an upstream-labeled binary.

## What the tests prove

These are supplementary engine-source consumers for `MH-EFFORT-001`.
The existing registered Avibe scenario still owns gateway/resolver evidence;
no new shared scenario identifier or shipped-engine claim is introduced.

- Shared policy covers unresolved/user-defined, configured unknown, static
  nil, supported, narrow and empty metadata; budget/level/future/none/absence;
  prepared-target authority; source-aware conversion; aliases and suffixes.
- Actual manager plus Claude HTTP executor tests bind two conflicting model
  snapshots to the selected credentials and aliases, then replace one key
  and metadata snapshot without changing routing. Both stream modes run.
- Fake subscription tests cover Claude and Codex HTTP, Codex WebSocket,
  Kimi Messages delegation and native/legacy Chat, and xAI Responses.
  The xAI final-egress matrix has 80 cases; the Kimi native/legacy matrix
  has 40. Payload override/filter, suffix priority, forced Claude tool choice
  and actual upstream cancellation remain consuming assertions.
- `wire_matrix.py` reuses `tests/e2e/drivers/mock_llm_upstream.py`. It creates
  actual task-owned `SourceRecord`/credential state and uses the frozen
  `CLIProxyEngineAdapter.sync_sources` and `EngineSupervisor`, with their real
  validation, transaction, transport barrier, atomic config writer and health
  checks. Existing installer/process/port constructor seams select only the
  preverified task binary, task environment and log, and a fresh private port.
  The 380-case three-protocol matrix asserts exact endpoint,
  model, selected fake key, non-ASCII text and reasoning fields, with no
  request sent to another Source. Four profiles use untouched generated
  registrations; the empty-object profile is explicitly engine-only and
  does not claim Avibe generates empty capability objects.
- Further consuming cases compare complete long UTF-8 model identities
  (including shared heads/distinct tails and route-only models) through
  registration and HTTP egress. Three sequential ~42 MiB four-image requests
  compare exact valid PNG/base64 data and text in native protocol shapes.
  Only the mock's test receive budget increases to 48 MiB for these cases.
  Allowed envelope/cache normalization is not mistaken for image truncation.
- Source origin/key replacement stops the prior owned child and starts a new
  one through the real product transaction. Requests consume the returned
  connection's port/token; registrations and exact model/Source identity remain.
  Focused actual-child regressions reject mismatched Source credentials before
  restart, inject a real exiting child to verify projection rollback and
  recovery, and verify failed-startup cleanup. `lifecycle.json` records these
  assertions and reaped child outcomes and is bound into the wire receipt.
  HTTP cancellation and same-source reuse complement the precise Go
  upstream-context cancellation test. All test processes and mock threads
  are stopped and reaped.

The former watcher-only fixture failed on Linux after the atomic config
replacement. The pinned engine watches the config file itself; no raw inotify
trace was captured. This is a separate diagnostic limitation, not an
established Avibe replacement regression: Avibe owns an explicit restart
transaction. The recipe neither changes the watcher nor writes config in place
or extends a replacement deadline. The engine-only empty-object profile is
applied after each real config generation, before each task child launch.

`--local-model` alone does not disable the upstream Antigravity version
updater. The private network and dead loopback proxy are intentional complementary
controls. No login, refresh, cloud account, user engine, or installed
runtime-state probe is part of this recipe.

Keep task evidence outside the repository. Go/package caches require a few
GiB; run the image fixtures and production builds sequentially. The recipe
retains per-run wire artifacts for inspection and does not perform cleanup
outside its explicitly supplied task directory.

## Maintenance and release boundary

Edit only a task-owned exact-base engine checkout, including the consuming
tests. Regenerate `native-intent.patch` with `git diff --binary` (include new
test files with intent-to-add), refresh `patched-files.json`, and repeat
clean-apply, test, build and wire checks. Review the diff for unrelated edits.
Never refresh catalogs from a moving branch to make a test pass.

Upstream's MIT notice is retained in `UPSTREAM-LICENSE`.

Shipping requires a separate source/provenance decision: authorized upstream
acceptance and release, or an explicitly approved Avibe-maintained source and
equally strong provenance contract. Then build/verify all four production
targets, publish available assets, satisfy the unchanged manifest guard, and
only then update an Avibe dependency pin. Publication, guard changes, releases,
installation, or runtime replacement are not authorized by this directory.
