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

Run from an Avibe checkout containing this directory. The verified envelope is
macOS with `sandbox-exec`, Git, Go, uv and Python 3.11+. It refuses an unsupported
host rather than running tests without egress isolation. Linux/Windows
isolation and four-platform release builds are pending, not emulated here.

Create a fresh task directory; never reuse an unexplained checkout or cache:

```sh
engine_task=$(mktemp -d)
git init "$engine_task/source"
git -C "$engine_task/source" fetch --depth=1 \
  https://github.com/router-for-me/CLIProxyAPI.git \
  2a6b87aca083a5bf498ac1f68a1b636c500d7aaa
git -C "$engine_task/source" checkout --detach FETCH_HEAD
mkdir -p "$engine_task/state/home" "$engine_task/state/tmp"
```

Only prerequisite downloads use public networking. The following Go command
downloads the exact toolchain and locked modules into task-owned caches.
It neither refreshes model catalogs nor accesses a provider account:

```sh
env -i PATH="$PATH" HOME="$engine_task/state/home" \
  TMPDIR="$engine_task/state/tmp" GOENV=off GOTOOLCHAIN=go1.26.4 \
  GOPATH="$engine_task/state/go" GOMODCACHE="$engine_task/state/mod" \
  GOCACHE="$engine_task/state/cache" GOMAXPROCS=2 GOMEMLIMIT=1536MiB \
  go -C "$engine_task/source" mod download
```

The HTTP fixture imports the actual Avibe registration writer and state store.
Use the existing `uv.lock`, without installing Avibe or executing a backend:

```sh
env -i PATH="$PATH" HOME="$engine_task/state/home" \
  XDG_CONFIG_HOME="$engine_task/state/config" \
  UV_CACHE_DIR="$engine_task/state/uv-cache" \
  UV_PROJECT_ENVIRONMENT="$engine_task/state/venv" UV_LINK_MODE=copy \
  uv --no-config sync --frozen --no-install-project --no-dev --no-managed-python
```

Apply once to a clean exact-base checkout. Reapplying to a dirty checkout is
refused; it never resets, stashes, or overwrites someone else's changes:

```sh
"$engine_task/state/venv/bin/python" patches/cliproxyapi/verify.py apply \
  --source "$engine_task/source" --state "$engine_task/state"
"$engine_task/state/venv/bin/python" patches/cliproxyapi/verify.py test \
  --source "$engine_task/source" --state "$engine_task/state"
"$engine_task/state/venv/bin/python" patches/cliproxyapi/verify.py build \
  --source "$engine_task/source" --state "$engine_task/state"
"$engine_task/state/venv/bin/python" patches/cliproxyapi/verify.py wire \
  --source "$engine_task/source" --state "$engine_task/state"
```

`inputs.json` freezes source identity, both committed catalogs, `go.sum` and
Go version. `patched-files.json` verifies the entire changed-file inventory
and bytes; unrelated edits/untracked files fail closed. The build receipt
binds the binary digest to the exact patch, toolchain and inputs. Wire tests
refuse a stale or replaced binary. Tests/builds use `GOPROXY=off`, checksum
verification, `-mod=readonly`, `GOMAXPROCS=2`, `-p=1`, isolated HOME/XDG/cache,
and loopback-only OS egress. The Go memory setting is a soft limit, not an
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
  actual task-owned `SourceRecord`/credential state and calls Avibe's real
  config writer. The 380-case three-protocol matrix asserts exact endpoint,
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
- Source origin/key replacement exercises the real config watcher.
  HTTP cancellation and same-source reuse complement the precise Go
  upstream-context cancellation test. All test processes and mock threads
  are stopped and reaped.

`--local-model` alone does not disable the upstream Antigravity version
updater. The OS sandbox and dead loopback proxy are intentional complementary
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
