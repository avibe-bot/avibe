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

Use an explicitly authorized, already-running Linux development guest with
Git, Python 3.12+, util-linux (`unshare`, `mount`, `setpriv`), iproute2 and
transient sudo. This recipe does not provision a guest or change packages,
services, routes or Incus resources. Its macOS `sandbox-exec` path now denies
all egress and is only for pure-source or compile diagnostics; network suites
have no macOS fallback.

Acceptance status: 225 pure fixture/receipt/probe/connection tests pass, and actual
no-network, private-loopback and deliberate nonzero probes have the expected
results. All three Linux Go suites and the network-none diagnostic build
passed. The first Linux wire run passed 380 policy, 24 identity and three
large-image cases, then failed its watcher-only replacement deadline.
The corrected real adapter/supervisor lifecycle rerun passed all three phases,
including the complete wire matrix, replacement/rollback/startup failure
consumers, cancellation/reuse and cleanup. Independent orchestrator source
acceptance and current-head PR gates remain required. Historical
macOS tests allowed arbitrary host loopback; they do not prove the repaired
test-owned listener isolation.

Set `engine_task` to the explicitly allocated guest scratch, not a user home,
shared checkout or broad temporary-directory root. Copy this directory into
`"$engine_task/recipe"` without AppleDouble files or Python caches. Preserve
any existing checkout/evidence; use new task children when needed:

```sh
git init "$engine_task/source"
git -C "$engine_task/source" fetch --depth=1 \
  https://github.com/router-for-me/CLIProxyAPI.git \
  2a6b87aca083a5bf498ac1f68a1b636c500d7aaa
git -C "$engine_task/source" checkout --detach FETCH_HEAD
mkdir -p "$engine_task/state/home" "$engine_task/state/tmp"
```

Only prerequisite preparation may use public networking. `prerequisites.json`
records exact Linux arm64 Go 1.26.4 and uv 0.9.8 archive URLs and SHA256 values.
Download into scratch, verify hashes before extraction, and extract Go into
`"$engine_task/toolchain"`. Do not install system packages or use a moving
toolchain. Download locked Go modules using that Go; no catalog refresh or
provider account is involved:

```sh
env -i PATH="$engine_task/toolchain/bin:/usr/bin:/bin" \
  HOME="$engine_task/state/home" \
  TMPDIR="$engine_task/state/tmp" GOENV=off GOTOOLCHAIN=go1.26.4 \
  GOPATH="$engine_task/state/go" GOMODCACHE="$engine_task/state/mod" \
  GOCACHE="$engine_task/state/cache" GOMAXPROCS=2 GOMEMLIMIT=1536MiB \
  go -C "$engine_task/source" mod download
```

Export the complete tracked Avibe fixture. Set `avibe_checkout` to a repository
containing the declared base; its dirty, staged and untracked files are ignored.
If exporting outside the guest, transfer the export metadata-free and retain
its actual guest path as `fixture_root`:

```sh
fixture_root=$(python3 -B - "$avibe_checkout" "$engine_task" <<'PY'
import json, sys
from pathlib import Path
recipe = Path(sys.argv[2]) / "recipe"
sys.path.insert(0, str(recipe))
from fixture import export_fixture
root, identity = export_fixture(
    Path(sys.argv[1]), Path(sys.argv[2]),
    json.loads((recipe / "inputs.json").read_text()),
)
print(root)
PY
)
```

The exporter checks exact commit tree, archive and complete path-independent
source digest. Actual config writer/state/mock imports use only this export,
never current-worktree `sys.path`. Build the venv from its frozen `uv.lock`,
without installing Avibe or running a backend. Set `uv_binary` to the verified
extracted uv executable:

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
"$engine_task/venv/bin/python" -B "$engine_task/recipe/verify.py" apply \
  --source "$engine_task/source" --state "$engine_task/state"
```

The namespace envelope is mandatory. Inspect its exact source, then run
`/bin/true` in `none` and `loopback` modes plus `/bin/false` to confirm failed
receipt/cleanup behavior. Use new receipt names every time; collisions fail
closed and preserve prior evidence. Example:

```sh
sudo -n /usr/bin/python3 -B "$engine_task/recipe/namespace.py" \
  --root "$engine_task" --source "$engine_task/source" \
  --fixture "$fixture_root" --state "$engine_task/state" \
  --recipe "$engine_task/recipe" --toolchain "$engine_task/toolchain" \
  --python-env "$engine_task/venv" --network none \
  --receipt probe-none-unique.json -- /bin/true
```

After independent probe acceptance, the following narrow helper runs the
maintained phases with the same mounted inputs. Build uses `none`; test/wire
use the private loopback. Run phases sequentially:

```sh
run_phase() {
  sudo -n /usr/bin/python3 -B "$engine_task/recipe/namespace.py" \
    --root "$engine_task" --source "$engine_task/source" \
    --fixture "$fixture_root" --state "$engine_task/state" \
    --recipe "$engine_task/recipe" --toolchain "$engine_task/toolchain" \
    --python-env "$engine_task/venv" --network "$2" --receipt "$3" -- \
    "$engine_task/venv/bin/python" -B "$engine_task/recipe/verify.py" "$1" \
    --source "$engine_task/source" --state "$engine_task/state" \
    --fixture "$fixture_root"
}
run_phase build none build-unique.json
run_phase test loopback test-unique.json
run_phase wire loopback wire-unique.json
```

The private network has only its own loopback: dynamic Go `httptest`, engine
and replacement mock listeners share it, while guest/host listeners and
external networks are unreachable. Mount propagation becomes private before
any bind. Source, fixture, recipe and toolchain are read-only; task state is
writable. Guest/user homes and namespace handles are hidden. Child code runs
without capabilities or supplementary groups, with no-new-privileges and no
inherited supervisor descriptors. PID 1 exit destroys remaining descendants.

Parent receipts live under `"$engine_task/receipts"`, outside child mounts.
They are exclusively opened before execution. Original preflight travels
through an unlinked supervisor descriptor closed before candidate launch;
mutable child-state artifacts and candidate stdout cannot replace it. Keep
parent receipts and before/after input/lifecycle evidence outside candidate
state. Both unrelated outside IPv4/IPv6 sentinels must observe zero connections.
A failed preflight, command or cleanup is never a passing receipt.

`inputs.json` freezes source identity, both committed catalogs, `go.sum` and
Go version, plus Avibe's commit/tree/archive/complete source digest.
`patched-files.json` verifies the entire changed-file inventory
and bytes; unrelated edits/untracked files fail closed. The build receipt
binds the binary digest to the exact patch, toolchain, fixture and recipe.
Fixture and source are verified before and after execution. Wire tests
refuse a stale or replaced binary. Tests/builds use `GOPROXY=off`, checksum
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
