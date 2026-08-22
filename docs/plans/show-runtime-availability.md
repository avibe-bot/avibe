# Show Runtime Availability

Status: proposed
Owner: unassigned
Repos touched: `avibe`, `avibe-bot/vibe-show-runtime`
New artifact: `avibe-show-runtime` PyPI distribution (see W3)

## 1. Background

Show Page is a core Avibe capability, but the managed Show Runtime that serves it
is delivered through a channel that is not as reachable as the one that delivers
Avibe itself. A single transient DNS failure during an upgrade left Show Page
permanently broken on a developer machine until it was repaired by hand.

### 1.1 Observed incident

Timeline on one macOS machine, upgrading to `avibe-os 3.0.13rc3`:

1. The upgrade replaced the wheel. The new wheel pins a *rebuilt*
   `darwin-arm64` archive (`a9c3ef28…`) even though `runtime_version` is
   unchanged (`64e2e610e4107304bdc8ce2f2d387d76be6f0a0b`).
2. The service restarted. The previous Show Runtime sidecar self-terminated
   (`parent process exited; shutting down orphaned server`).
3. The startup dependency reconcile treated the already-working local install as
   "not installed" (see §2.1) and began a mandatory ~21 MiB download.
4. `github.com` DNS resolution failed three times:

   ```
   Dependency request attempt 1/3 failed for
   https://github.com/avibe-bot/avibe/releases/download/gh-v3.0.13rc3/vibe-show-runtime-node-darwin-arm64.tgz (dns)
   ...
   core.dependency_network.DependencyNetworkError: DNS lookup failed for github.com
   Startup dependency reconcile completed with issues: show_runtime=runtime_archive_download_failed
   ```

5. `_install_attempted` latched for the process lifetime. Every later `ensure()`
   short-circuited: no retry on request, no background retry. `ui_stderr.log`
   mtime stayed frozen while later `/show/` requests were served.
6. Every `/show/` request fell through a bare `except Exception` to the recovery
   HTML, whose spinner has a 30 s CSS delay — presenting as an endless spinner.
7. Network was healthy minutes later. The runtime never installed until the
   operator clicked install in Settings.

### 1.2 Delivery-channel asymmetry

`install.sh` already treats weak-network users as a first-class case for the
Python package (`install.sh`, `install_avibe`):

```sh
# Try in order: PyPI -> China mirror (tsinghua) -> GitHub
if   install_package_candidate "$PACKAGE_NAME"; then                  # PyPI
elif install_package_candidate "$PACKAGE_NAME" --index-url https://pypi.tuna.tsinghua.edu.cn/simple
elif install_package_candidate "git+https://github.com/${REPO}.git"
```

The Show Runtime archive has no equivalent. It is fetched from a single
hard-coded `github.com` release URL with no mirror and no fallback. A user who
can install Avibe cannot necessarily obtain its Show Runtime. This is a designed
asymmetry, not an accident of the incident above.

## 2. Root causes

Five independent layers had to hold for the outage to occur. Each is separately
fixable.

### 2.1 Install identity is over-broad

`core/show_runtime.py`, `_manifest_install_dir`:

```python
fingerprint = hashlib.sha256(f"{manifest.digest}:{archive.sha256}".encode("utf-8")).hexdigest()[:16]
```

`_manifest_install_matches` requires all five fields to match, including
`manifest_sha256 == manifest.digest`.

`manifest.digest` is the digest of the **whole manifest file**, which describes
all six platforms. Any manifest edit — including one that only rebuilds
`win32-x64` — changes that digest and therefore invalidates the install on every
other platform. macOS and Linux users would re-download ~21 MiB for a
Windows-only change, with nothing about their own runtime changed.

### 2.2 The runtime bundle is not reproducible, and `runtime_version` is an incomplete identity

`vibe-show-runtime/scripts/bundle-vibe-remote.mjs` writes a fresh
`package.json` into a temp stage directory using floating ranges, then runs
`npm install` (not `npm ci`) in a directory that has no lockfile:

```js
dependencies: {
  "@avibe/show-runtime": "file:./packages/runtime",
  "@avibe/show-ui": "file:./packages/ui",
  "@avibe/show-sdk": "file:./packages/sdk",
  "@vitejs/plugin-react": "^5.1.1",
  react: "^19.2.0",
  "react-dom": "^19.2.0",
  vite: "^7.2.4"
}
```

The repo's committed `package-lock.json` governs the repo's own build
dependencies; it does not govern the bundled `node_modules`. Consequences:

- Two bundles built from the same source commit can contain **different**
  Vite/React/plugin versions. The 1800-byte delta between the two archives
  observed on disk is very likely a real content difference.
- `runtime_version` (the `vibe-show-runtime` commit SHA) therefore does **not**
  fully identify a bundle. `archive.sha256` is the only complete identity.
- Byte churn with no source change is expected rather than exceptional, so the
  identity check in §2.1 fires routinely.

The archive is then created with a shell `tar -czf`, which records mtimes and
directory traversal order — a second, independent source of nondeterminism.

Note: the archive contains no Node binary. It bundles `package.json`,
`package-lock.json`, `packages`, and `node_modules`; per-platform builds exist
because `node_modules` carries platform-native binaries (esbuild, rollup,
lightningcss). The runtime uses the *system* Node, constrained by the manifest's
`minimum_node` (`^20.19.0 || >=22.12.0`).

### 2.3 One failure latches for the process lifetime

`core/show_runtime.py`:

```python
self._install_attempted = False
...
if self.auto_install and not self._install_attempted:
    self._install_attempted = True
    command = await asyncio.to_thread(self._install_managed_runtime)
```

A single failure — transient or not — disables every subsequent install attempt
until the process restarts. The module already holds the correct convention a few
lines above, used only by capability negotiation:
`_capability_retry_deadline`, `_capability_retry_attempt`, and
`_show_runtime_capability_retry_delay`.

### 2.4 The archive has exactly one network source

`_resolve_manifest_archive` (the default `manifest-cache` path) has two sources:
the local `downloads/<sha256>.tgz` cache, and a network GET of `archive.url`.
On a machine that has never held the archive, that single GET is the whole
delivery mechanism, and it points at one hard-coded `github.com` host.

`_copy_packaged_runtime_archive` — which reads a wheel-bundled archive from
`vibe/show_runtime/` — is only reachable from the legacy `archive` source path
(`_resolve_prebuilt_archive`). The default path never consults it.

The wheel ships only `vibe/show_runtime_manifest.json` (~2 KiB) plus an empty
`vibe/show_runtime/.gitkeep`. The release pipeline actively asserts the archives
are absent (`.github/workflows/publish.yml`, and the same check in
`release_ai.yml`):

```python
if archives:
    raise SystemExit("Wheel unexpectedly contains Show Runtime archives:\n" + ...)
```

`AGENTS.md` §9 currently states that GitHub pre-release wheels carry
`bundled vibe/show_runtime/*.tgz`. That contradicts the enforced pipeline and is
stale.

### 2.5 The failure is invisible and the recovery page misleads

Both `/show/` call sites wrap `_show_page_runtime_response` in a bare
`except Exception` and log at `logger.debug`, discarding the reason code. The
incident appears nowhere in `vibe_remote.log`.

`show_page_runtime_recovery_html` takes no `reason` parameter. It renders
"Ready to visualize" plus a copyable prompt telling the agent to replace
`src/App.tsx` and check the Vite console — actively wrong for an infrastructure
failure. `SHOW_RUNTIME_RECOVERY_LOADING_DELAY_SECONDS = 30` delays the state
change by 30 s and is simultaneously passed to the node CLI as
`--fallback-delay-seconds`, conflating two unrelated concerns.

## 3. Current update strategy (for reference)

- The runtime version is locked to the Avibe version. The manifest is baked into
  the wheel; there is no independent runtime update, no version discovery, and no
  floating "latest" on the default `manifest-cache` source. Non-default sources
  are inconsistent here: `_RUNTIME_GITHUB_REF = "main"` and
  `releases/latest/download` do float.
- Update trigger is identity mismatch (§2.1), evaluated by the one-shot startup
  dependency reconcile after a restart.
- `core/update_checker.py` `_perform_update` calls `do_upgrade` and restarts. It
  never touches Show Runtime. The runtime re-download is therefore a silent
  side effect of an Avibe auto-update, executed at the moment the previous
  sidecar has just been killed.
- Retention is `_MANAGED_RUNTIME_ROLLBACK_INSTALLS = 1`: the current install plus
  one previous install are kept, and the `downloads/` archives those two need are
  protected from GC.
- The switch is not atomic. The new identity becomes authoritative before its
  bytes are known to be obtainable. Nothing consumes the retained install.

## 4. Goals

1. A completed Avibe install implies a working Show Page, over the same channel
   that delivered Avibe.
2. A transient failure self-heals without a service restart.
3. A runtime rebuild with no source change does not force any user to
   re-download.
4. A manifest change confined to one platform does not invalidate other
   platforms' installs.
5. When the runtime genuinely cannot be obtained, the user is told the truth and
   given a command that can work.
6. No single host, registry, or vendor can make the runtime unobtainable, and
   every tier that carries it is one a third party already mirrors for us.

## 5. Non-goals and rejected options

**Rejected: rolling back to a different `runtime_version`.** The runtime's
internal packages are all `"version": "0.0.0"`; the only version identity is the
commit SHA. Compatibility ranges cannot be expressed. Avibe's scaffold imports
`@avibe/show-ui` and `@avibe/show-sdk`, which ship *inside* the runtime bundle, so
an older runtime can be missing exports the current scaffold requires. Serving a
mismatched runtime is not a degraded success; it is an unverifiable failure.
Never reuse an install whose `runtime_version` differs from the pinned one.

**Rejected: reusing a same-`runtime_version` install with different archive
bytes.** Considered and dropped once §2.2 was understood. Because the bundle's
third-party dependencies are resolved at build time and are not captured by
`runtime_version`, two archives with the same `runtime_version` are not known to
be equivalent. The correct fix is to make the archive deterministic upstream
(W1), not to add an equivalence heuristic downstream.

**Rejected: one universal wheel carrying all six platforms.** 9.06 MiB (current
wheel) + 140.8 MiB (six platforms, all managed runtimes) ≈ 150 MiB, over PyPI's
100 MiB per-file limit.

**Rejected: six platform-specific wheels of `avibe-os` carrying the archives.**
This was the first version of W3. It fails on cost/benefit. The runtime changes
about twice a month; `avibe-os` releases about six times a month. The archives
would therefore be re-uploaded, unchanged, on most releases:

| | Bytes |
| --- | --- |
| Uploaded over the measured 74 days | 15 releases × 6 platforms × ~21.3 MiB = 1.87 GiB |
| Distinct archive content in that window | ~3 runtime versions × 6 × ~21.3 MiB = 383 MiB |
| Re-uploaded identical bytes | ~1.5 GiB (~80%) |

It also carries permanent complexity that a source ladder does not need: platform
tags in `hatch_build.py`, a 6× wheel fan-out in two workflows, a measured glibc
floor, an inverted pipeline assertion, and six extra release artifacts forever.
And it addresses only §2.4, by routing around the single source rather than
removing it — a machine that installs the pure fallback wheel is left with
exactly today's single-source path.

**Rejected as the first tier: a self-hosted CDN (Cloudflare R2 or similar).**
Cheap to operate, and it is the only option that needs no version scheme at all —
objects can be keyed directly by commit SHA, which is a real advantage over any
registry. But the entire motivation for this plan is users who cannot reach
`github.com`, and a self-hosted origin is the one option whose China reachability
we would have to solve, and pay for, ourselves. PyPI and npm are already mirrored
in full, by third parties, at no cost to us. A self-hosted tier stays available as
an *additional* tier if origin control is later wanted; it must not be the first
one.

**Out of scope for this plan:** bundling `memory_runtime` (its manifest has
placeholder `size: 1` entries and looks unfinished) and `model_hub_runtime`
(different manifest shape).

## 6. Size budget

Measured from the manifests on `master`:

| Runtime | darwin-arm64 | linux-x64 | win32-x64 |
| --- | --- | --- | --- |
| show | 22302344 | 23242796 | 24113099 |
| git | 1684366 | 1765892 | n/a |
| tmux | 623837 | 958795 | n/a |
| **per-platform total** | **~23.5 MiB** | **~24.8 MiB** | **~23.0 MiB** |

Six-platform total: 140.8 MiB. Show Runtime is the overwhelming majority; git and
tmux together are ~2.5 MiB and are nearly free to include.

Current PyPI state: `avibe-os` 3.0.12 is a 9.06 MiB `py3-none-any` wheel plus an
8.79 MiB sdist; 15 releases occupy 0.18 GiB of the default 10 GiB project quota.

Measured cadence over the same 74 days: `avibe-os` shipped 15 releases (~6.1 per
month), while `vibe-show-runtime` `main` took 5 commits (~2 per month) clustered
into roughly 3 distinct runtime versions. The two cadences differ by ~3×, which is
what makes a separate distribution the right shape (§5).

Projected under W3:

| Distribution | Per upload | Cadence | Per year | Default 10 GiB lasts |
| --- | --- | --- | --- | --- |
| `avibe-os` — unchanged: pure wheel + sdist | 17.85 MiB | ~6.1/month | 1.28 GiB | ~7.6 years |
| `avibe-show-runtime` — 6 platform wheels + 1 pure fallback | ~130 MiB | ~2/month | ~3.1 GiB | ~3.2 years |

Each distribution carries its own 10 GiB project quota, and the largest single
file is ~22 MiB against a 100 MiB per-file limit. **No quota increase is required
to land W3.** The ~130 MiB figure is an estimate from the three measured
platforms; the implementer should record the exact six-platform total on the first
release, and request an increase later from real usage rather than a projection.

## 7. Workstreams

### W1 — Deterministic runtime bundle (repo: `vibe-show-runtime`)

Two ordered parts. Part (a) is the substantive fix; (b) alone is insufficient.

**(a) Pin the bundled dependency set.** The staged `package.json` must resolve to
the same dependency tree for a given source commit. Either commit a lockfile for
the staged manifest and use `npm ci`, or replace the caret ranges with exact
versions. Prefer the committed lockfile: it also pins transitive dependencies,
which exact top-level versions do not.

**(b) Create the archive deterministically, cross-platform.** The build matrix
runs on `ubuntu-latest`, `ubuntu-24.04-arm`, `macos-15-intel`, `macos-14`,
`windows-latest`, and `windows-11-arm`. GNU-tar-only flags such as
`--sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner` work on the Ubuntu
runners and fail on the macOS and Windows ones (bsdtar). Use a Node
implementation instead — the `tar` npm package's `portable: true` option
normalizes mtime/uid/gid and omits the gzip mtime header — with an explicitly
sorted entry list. Record the new dependency in `package.json` (dependencies are
never added silently).

Platform-native binaries still legitimately differ per platform; determinism is
required per (platform, commit), not across platforms.

**Acceptance:** building the same commit twice on the same runner OS, in separate
clean checkouts, produces byte-identical archives. Add this as a CI check.

### W2 — Narrow the install identity (repo: `avibe`)

In `core/show_runtime.py`, remove the whole-manifest digest from both the
fingerprint and the match check:

```python
fingerprint = hashlib.sha256(
    f"{manifest.runtime_version}:{archive.platform}:{archive.sha256}".encode("utf-8")
).hexdigest()[:16]
```

and drop the `manifest_sha256` comparison from `_manifest_install_matches`.
Keep writing `manifest_sha256` into the install metadata for diagnostics.

This loosens nothing: `runtime_version`, `platform`, and `archive.sha256` all
remain exact-equality gates. It only stops unrelated manifest content from
participating in identity.

Migration: existing installs carry the old fingerprint in their directory name.
`_legacy_manifest_install_dir` already establishes the precedent for tolerating a
prior layout — extend the same treatment so an install that matches on the three
retained fields is adopted in place rather than re-downloaded. This must not
weaken `_protected_archive_sha256s` or `_clean_manifest_install_dirs`: an adopted
install and its archive must be protected from GC.

**Acceptance:** a manifest edit confined to one platform leaves other platforms'
installs valid. An install created by the previous fingerprint scheme is adopted
without a download.

### W3 — Multi-source archive delivery

Repos: `avibe`, `vibe-show-runtime`, plus a new `avibe-show-runtime` PyPI
distribution. `avibe-os` stays a single `py3-none-any` wheel throughout.

Replace the one hard-coded `github.com` GET with an ordered ladder. Every tier
either is local or is a registry a third party already mirrors in full:

| Tier | Source | Weak-network reachability | Work |
| --- | --- | --- | --- |
| 1 | `avibe-show-runtime` resolved as a dependency at install time | Tsinghua and Aliyun mirror PyPI in full, and `install.sh` already walks that ladder for `avibe-os` itself | W3.2 |
| 2 | local `downloads/<sha256>.tgz` | local | exists |
| 3 | npm registry, then `registry.npmmirror.com` | first-class China mirror, identical path layout | W3.3 |
| 4 | GitHub release asset | poor — today's only tier | exists |

Tier 1 is what satisfies goal 1 ("a completed install implies a working Show
Page"), and it needs no new infrastructure: pip resolves it through the same
index, and the same mirror, that delivered `avibe-os`. Tiers 3 and 4 cover
platforms tier 1 has no wheel for, sideloaded wheels, and installs that bypassed
the index.

Ladder order in `_resolve_manifest_archive` is tier 2 → tier 1 → tier 3 → tier 4:
both local tiers come first, and the already-materialized cache before the
packaged copy so a hit costs no extraction.

#### W3.0 — Give the runtime a real version (repo: `vibe-show-runtime`)

Prerequisite for W3.2 and W3.3, and the reason the CDN option in §5 looked
cheaper. Today `@avibe/show-runtime`, `@avibe/show-ui`, and `@avibe/show-sdk` are
all `"version": "0.0.0"`, and the commit SHA is the only identity — which neither
PyPI nor npm will accept as a version.

Recommended scheme: date-based `0.<YYYYMMDD>.<n>`, where `n` counts releases
within the day. One string is simultaneously a valid PEP 440 version and valid
semver, so PyPI and npm publish under the same version. The commit SHA stays the
audit identity: it remains `runtime_version` in the manifest, and is recorded in
both packages' metadata. The implementer confirms or replaces this scheme, and
records the decision here.

**Acceptance:** a tagged runtime release produces one version string accepted by
both registries, and the manifest for that release records both the version and
the commit SHA.

#### W3.1 — Source ladder in the resolver (repo: `avibe`)

Restructure `_resolve_manifest_archive` into an ordered list of candidate sources
with one shared contract per candidate:

- verify the manifest's `size` and `sha256` before the bytes are used;
- on any failure — unreachable, corrupt, absent, checksum mismatch — log at
  `warning` with the source name and the reason, then fall through to the next
  candidate. Never fail closed on one bad source.
- on success, materialize at `downloads/<sha256>.tgz` and record which source won
  in the install metadata, so a support question has an answer.

Only when every candidate has failed does the reason code propagate to W4/W5.

Inventory the existing `VIBE_SHOW_RUNTIME_SOURCE` implementations first — there
are already four (`manifest-cache`, `archive`, `github`, `npm`) — and extend that
machinery rather than adding a parallel path. Per the reuse ladder, generalizing
`_copy_packaged_runtime_archive` is preferred over a new helper.

This PR is independently valuable: it removes the single point of failure using
only tiers that exist today, before any new distribution is published.

**Acceptance:** with the GitHub host unreachable but npm reachable, a cold install
obtains the archive. A deliberately corrupted candidate is skipped, not fatal, and
the fallthrough is visible in the log.

#### W3.2 — `avibe-show-runtime` distribution (repos: `vibe-show-runtime`, `avibe`)

**Layout.** Per runtime version, publish six platform wheels — each carrying
exactly one platform's Show Runtime archive — plus one `py3-none-any` wheel that
carries **no** archive. Wheel-tag priority makes installers prefer the platform
wheel; musl, BSD, and any future target resolve the pure wheel instead. That pure
wheel is what keeps resolution from hard-failing on an unmatched platform: it
installs successfully, contributes no archive, and the resolver falls through to
tier 3. This is the same fallback-tier trick the rejected design used at the
`avibe-os` level, relocated to where it costs nothing.

Platform tag mapping must be decided explicitly and recorded here by the
implementer (`darwin-arm64` → `macosx_11_0_arm64`, `linux-x64` →
`manylinux_2_17_x86_64`, `win32-x64` → `win_amd64`, and so on), with the glibc
floor chosen to match what the bundled native binaries actually require. This is
the one piece of the rejected design that survives — but it lives in a small new
project instead of in `avibe`'s build hook.

**Dependency.** `avibe-os` declares `avibe-show-runtime==<version>` as an ordinary
pinned dependency, with no environment markers: the pure fallback wheel makes
markers unnecessary. Keep the pin a literal in `pyproject.toml` — do not generate
it at build time — and add a CI check asserting it equals the version recorded in
`vibe/show_runtime_manifest.json`. `hatch_build.py` already validates rather than
generates; follow that precedent.

The pin is the mechanism that makes runtime/scaffold compatibility
resolver-enforced instead of implicit in a manifest, which is what §5's rollback
rejection is really asking for.

**Resolution.** Tier 1 reads
`importlib.resources.files("avibe_show_runtime") / <archive-name>`, validates
`size` and `sha256`, and on success copies to `downloads/<sha256>.tgz`. An
`ImportError` or a missing file is an ordinary fallthrough, not an error — the
pure wheel hits this path by design.

**Pipeline.** The existing assertion in `publish.yml` and `release_ai.yml` — that
the `avibe-os` wheel contains no `vibe/show_runtime/*.tgz` — is **correct and
stays**. Add the mirror-image checks in the runtime project: each platform wheel
contains exactly its own platform's archive, with a sha256 equal to the manifest
entry, and the pure wheel contains none.

**Acceptance:** on a supported platform, `uv tool install avibe-os` with all
network egress blocked *after* installation yields a working Show Page with no
download. On an unsupported platform the install still succeeds and degrades to
tiers 3–4.

#### W3.3 — npm tier (repos: `vibe-show-runtime`, `avibe`)

Publish the per-platform archives to npm under `@avibe/show-runtime-<platform>`,
following the established `os`/`cpu`-scoped pattern used by esbuild and swc.

One trap: npm re-tars on publish, so the registry tarball's own sha256 will
**not** equal the manifest's `archive.sha256`. Carry our archive as a file inside
the npm package and verify that inner file's sha256 after extraction. This needs
no manifest schema change.

Mirroring is a base-URL swap — `registry.npmjs.org` → `registry.npmmirror.com`,
identical path layout — expressed as an ordered list of base URLs with an env
override, not as a new manifest field.

**Acceptance:** with `registry.npmjs.org` blackholed, the npmmirror base URL
serves a verified archive.

#### W3.4 — Docs (repos: `avibe`, `avibe-docs`)

- Correct the stale `AGENTS.md` §9 sentence. Under W3 there is nothing to bundle
  into a pre-release wheel; a GitHub-only pre-release resolves its runtime from
  the pinned `avibe-show-runtime` version, which in most cases is already
  published because the runtime cadence is ~3× slower than the release cadence.
- Add a user-facing weak-network / offline install page to `avibe-docs/` covering
  `vibe doctor repair show-runtime`, the source ladder and its env overrides, and
  the `VIBE_SHOW_RUNTIME_ARCHIVE_PATH` / `_ARCHIVE_URL` / `_MANIFEST_PATH` /
  `_MANIFEST_URL` / `_OFFLINE` and `VIBE_INSTALL_SKIP_SHOW_RUNTIME` escape
  hatches. Today `avibe-docs/` mentions none of them. Keep English and Chinese in
  1:1 correspondence.
- `VIBE_SHOW_RUNTIME_SOURCE=npm` becomes documentable once W3.3 ships; it must
  stay undocumented until then, because `@avibe/show-runtime` is not on the
  public registry today.

### W4 — Bounded retry instead of a lifetime latch (repo: `avibe`)

Replace `_install_attempted` with bounded, request-driven retry records. The
failure model has five values because the available evidence does not always
prove whether a condition will heal:

- **transient** — archive or manifest download failures: exponential backoff,
  roughly 5 s → 5 min;
- **configured** — offline refusal and missing or unsupported local
  prerequisites (`node`, `git`, `npm`, or an explicit command): wait until the
  prerequisite evidence changes or the user explicitly retries;
- **permanent** — unsupported platform, source, or archive URL: wait for changed
  configuration or explicit intent;
- **checksum** — a verified checksum mismatch: wait for changed evidence or
  explicit intent;
- **unclassified** — startup URL/process/health failures,
  `runtime_proxy_failed`, generic install failures, and meaning-poor manifest or
  archive failures. Make exactly one timed confirmation attempt for the same
  evidence identity, then stop automatic attempts. Promoting this value to
  transient retries an actually permanent failure forever; promoting it to
  terminal abandons a recoverable failure. It is therefore a real third
  behavior, not a default branch.

Install-lock contention is not a member of that list. It proves another process
owns admission, so this operation did not run. The page may continue checking,
but the losing caller records no failure and installs no backoff caused by
Avibe's own concurrency.

The recovery surface must explain an unclassified stop in human copy: what
happened, that automatic retries have stopped after the confirmation, and the
concrete explicit action (`vibe doctor repair show-runtime` or **Retry now**)
that bypasses the gate. A reason token alone strands the user and is not an
acceptable refusal.

Retries are request-driven; introduce no new background timer. Then ensure all
three opportunities actually attempt installation: installer
(`install.sh` `prepare_show_runtime`, `install.ps1` `Prepare-ShowRuntime`),
startup reconcile (`vibe/api.py` `reconcile_startup_dependencies`), and the
`/show/` request path.

**Failure classification is published by the failure owner.** The install
dimension publishes its class together with the install reason; the runtime
dimension does the same for startup and proxy failures. The page consumes that
class and never rebuilds it from a reason string. Existing startup evidence can
prove whether the URL appeared, whether the child stayed alive, and whether
health answered, but it cannot prove whether the same observation will
self-heal; those reasons are therefore unclassified. `runtime_proxy_failed` has
the same limitation. Missing or too-old Node and missing git/npm are configured
because a local prerequisite change is the recovery event. Unsupported
platform/source/URL is permanent. Evidence-poor manifest and archive failures
remain unclassified rather than acquiring confidence they do not carry.

**A retry identity contains only operation preconditions.** The inclusion rule is
a predicate, not a remembered exclusion list: an input belongs in the identity
if and only if no Avibe operation writes it. Derive Avibe's write-set from the
code, then apply that predicate to every admission input. Re-resolve the result
before honoring any automatic backoff; it performs no network I/O. Included
inputs are source configuration, the manifest pin that ships in the wheel,
configured local source-file fingerprints, the user's explicitly configured
runtime executable fingerprint, and the Node/git/npm executable fingerprints.
The explicit runtime is external input because Avibe never writes it; a resolved
managed runtime is output because Avibe does. Excluded outputs include
`current.json`, a resolved managed runtime command, install metadata, and every
managed install tree: the operation writes or replaces them, so including them
makes its own side effects decide whether it may run again. "One confirmation
per identity" would then be unbounded in disguise. Product state answers whether
a runtime can be used; precondition evidence answers whether an operation may
run again. A healthy installed command bypasses the install gate entirely, which
already provides recovery after an out-of-process repair or pointer switch
without making product state part of the backoff identity.

**Classification vocabulary is shared; retry ownership is not.** Install and
startup have independent retry records because they answer different questions.
A failed install is not evidence about runtime startability. Every manager
admission decision receives intent explicitly: automatic callers pass
`automatic=True`, while explicit user actions pass false and bypass a live
install or startup gate unconditionally. Runtime transport failures remain a
separate dimension and publish typed evidence, but PR 2c deliberately carries no
request-level retry record or automatic browser poller; those move together to
the successor described below.

A forced replacement is accounted from the operation outcome (`ok`) only. If
replacement reports `ok=false` while a healthy existing command remains on disk,
only the replacement-operation backoff is recorded; no startup/use backoff is
installed, and `ensure()` continues serving the existing runtime. Replacement
failure and runtime unusability are independent facts.

**Retry behavior is published by the retry owner, not derived from failure
class.** The review breaker on PR #1640 found five instances of one property
failure in the retry work: a consumer manufactured an answer that an owner
should publish. A request header stood in for authorization; a retry write after
lock release stood in for the serialized admission outcome; failure class stood
in for retry eligibility; the spelling of a reason token stood in for the
dimension that owned it; and install-lock contention was promoted from "did not
run" to "ran and failed." The third instance was explicitly approved in an
earlier ruling: the page-side class-to-retry-mode table was praised as stronger
than the requested owner-published classification. It was the same forbidden
derivation one step later. Classification describes the evidence's cause;
retry disposition describes what the owner will do next. Neither is a proxy for
the other.

The install retry owner therefore publishes its disposition inside the install
dimension, and the start retry owner publishes independently inside the runtime
dimension. The request transport boundary publishes the four facts carried by
its typed unavailable error and never overwrites install or startup state. On
this branch a transport failure is non-terminal and the recovery page offers a
manual reload; it is structurally unable to publish `manual_only` for the request
dimension. The closed disposition vocabulary remains `continuous`,
`confirmation_pending`, and `manual_only`. The policy dimension publishes its
own `configured` class and `manual_only` disposition for both automatic-install
opt-outs. The page projects published values and never derives behavior from a
failure class, reason prefix, or transport header.

**Admission accounting is atomic with admission.** The provider result,
exception normalization, and retry-record write converge while the serialized
install guard is still held. A second admission cannot pass the retry gate or
acquire the guard without seeing the first outcome. A failed guard acquisition
is published as `not_applicable`, because no provider ran; only an operation that
actually ran and failed may create a backoff. Writing retry state after releasing
the guard, or checking the gate once before and again after acquisition, gives
the property two owners and reopens the race.

The next reviewed head exposed the same missing property in startup admission:
`Popen` and every earlier or later startup exception could escape after the gate
without publishing a retry outcome. The prior ruling had quantified atomic
admission over install only, even though it named start as a retry owner in the
next paragraph. This is the third governance error of the same form in this
workstream: head five bounded an artifact when a property was at risk; head six
bounded owners when the risk lived in their shared type; this ruling bounded one
dimension when the property covered every retry-owning dimension. Enumerating
visible sites is not a substitute for stating a property with its universal
quantifier.

The terminal rule is therefore independent of operation and dimension: for every
retry owner, one exception-closed span covers **evidence acquisition -> gate (when
one exists) -> attempt -> publication**, and every exit publishes exactly one
outcome through the same owner. Install publishes before releasing its serialized
guard; startup covers local precondition acquisition, workspace/cache
establishment, orphan cleanup, log creation, process spawn, startup URL discovery,
and health confirmation. Ordinary exceptions become structured failures.
Cancellation and other `BaseException` exits publish first and then propagate.
Success, failure, gate refusal, and exception paths all converge at one completion
publisher; no return or exception site writes retry state itself.

`not_applicable` means an attempt was not owed. Install-guard contention is the
canonical case because another holder is already making progress. `failed`
means an attempt was owed and did not succeed. A command that resolved and then
became missing or non-executable is a configured startup failure; other
establishment/readiness exceptions remain honestly unclassified, receive one
confirmation, and then name the explicit recovery action.

The exit inventory is executable evidence rather than a review assertion. An AST
test discovers every manager retry-record attribute and requires exactly one
writer for each; this owner set contains install and start, so a newly introduced
record makes the assertion fail. A separate gate census discovers every
`_active_retry` caller and proves that those same two owners are the only gated
dimensions. For each owner, the exit inventory requires one completion-publisher
call in a `finally`, one normal return after publication, one propagated
`BaseException` raise after publication, and a `BaseException` handler around the
attempt span. The proof becomes incomplete if retry storage no longer follows the
discovered attribute shape, a completion publisher gains fallible I/O, or an
operation gains another exit path; such a change must extend the inventory and
move the boundary rather than relying on the old assertion.

The fourth findings-bearing head exposed a deeper failure in how the prior proof
obligations were chosen. Each ruling named the right principle and then allowed
an example list, a type check, or an unnamed second path to stand in for the
principle's extent. The request path kept a second, unclosed exception route; the
precondition predicate was recorded as an extensional exclusion list and thereby
excluded a user-owned executable that Avibe never writes; and the page introduced
its own confirmation counter even though no earlier example had named that kind
of derived decision. Each proof obligation had been derived from the preceding
round's findings, so it was rigorous over exactly the domain that the prior round
had taught us to inspect and could not fail on the next round's defects. The AST
admission inventory is the concrete example: correct over `_active_retry`
callers, and provably blind to transport exception closure, explicit-command
fingerprints, and a browser-owned retry budget. Evidence that cannot fail outside
the last finding's domain is not evidence. This is the fourth governance
admission for PR #1640.

The runtime request span is exception-closed for evidence even though it owns no
retry record on this branch. `request()` and `request_global()` share one
manager-owned transport boundary. An ordinary transport exception publishes
reason, class, non-terminal disposition, and recovery action through
`ShowRuntimeUnavailableError` before reaching the UI. Reusing the start record is
forbidden: every request calls `ensure()`, whose healthy fast path clears startup
backoff immediately before transport, and successful startup is not evidence that
a request can be served.

The API's explicit total-deadline error remains a distinct
`ShowRuntimeRequestTimeoutError` response. A raw default `httpx` transport timeout
now becomes the structured unavailable error instead of escaping; this is a
deliberate caller-visible contract change, and tests assert its reason, class,
disposition, action, and original cause. Internal capability and health probes
close their own transport exceptions into their narrower capability/health
results. The UI catches only the two typed outcomes and projects the unavailable
error's four fields. Its former fallback, which fabricated all four values for an
arbitrary exception and could permanently stop a public page, is deleted. An
unknown programming exception remains loud rather than acquiring recovery
evidence no owner published.

Two censuses are merge evidence for the retained property:

1. Enumerate every recovery fact consumed by the page or an admission gate — the
   unavailable exception's reason/class/disposition/action, the corresponding
   response headers and document dataset, authorization plus explicit retry, and
   install/start retry identity/deadline. For every value, name exactly one producer; every
   other site is a projection, and defaulting branches are deleted rather than
   repaired. The executable census also enumerates every direct `AsyncClient`
   path in the manager and requires external requests to converge through the
   transport owner. It becomes incomplete if a new header, dataset key, gate
   input, exception route, or direct HTTP client appears without extending the
   census. No request retry record or automatic-poll header may appear in PR 2c.
2. Enumerate every retry-identity input and apply the write-set predicate above.
   The executable census fixes the complete set of manager attributes and helper
   inputs, proves that the manager assigns those attributes only during
   construction, and pairs two consuming tests: changing managed install output
   does not change identity, while replacing a user-configured executable does.
   It becomes incomplete if a helper begins reading a new fact or an Avibe
   operation begins writing an included path without extending the write-set.

The fifth findings-bearing head fired the pre-committed boundary. Its three
findings all landed inside properties PR #1640 claimed to own: the browser made
one more automatic request after the request owner published `manual_only`, local
fingerprint I/O ran synchronously on the ASGI path, and malformed user commands
escaped before admission ownership began. Master has no automatic poller, only a
manual Retry button. Therefore automatic request-level recovery — the browser
poll loop and the request retry record that exists only to bound it — leaves PR
2c as one property. Removing both restores master's manual behavior exactly;
retaining only one would either leave an unbounded loop or make a bound apply to
user-initiated work.

The identity findings stay and are fixed in PR 2c because install and startup are
their remaining consumers. Before consolidation each admission acquired identity
twice on opposite sides of its exception boundary. The gate could consult snapshot
B while publication keyed the record to snapshot A; a changed executable or file
between reads then made the bound silently fail. Each owner now acquires one
snapshot inside its exception boundary, uses that same value for gate and
publication, and startup performs blocking path resolution, stat, and hashing off
the event loop only when a start admission is actually needed. A healthy asset
request performs no retry fingerprinting. Malformed user-owned Runtime or Node
commands publish configured, manual-only evidence rather than escaping as a 500.

This exposed the fifth governance error in the boundary method. The previous rule
started ownership at the gate because gate, attempt, and publication were the
artifacts in view. It omitted the evidence that decides the gate. An owner's
exception boundary begins at the first read of state outside the owner, not at the
first action. Anything read before the `try` is unowned evidence on which the
published outcome may depend.

The corresponding proof is an AST-derived prologue inventory for every retry
owner. It enumerates every attribute read, method call, and free-function call
between the function's first statement and its `try`, with a reason why each
cannot fail or affect the outcome. The expected runtime-effectful prologue is
empty: only local variables initialized from literals may precede the boundary.
This proof would fail if a future owner resolved a command, read manager state, or
called a helper before entering `try`; it becomes incomplete if ownership moves
to a callable the inventory no longer discovers.

**Successor scope: automatic request-level recovery.** The successor is the only
PR that may reintroduce the automatic loop. It stacks on PR 2c and carries the
request retry owner with the loop because they are one property. It inherits the
following constraints in full:

The delivery record moves three resolved PR #1640 threads with that scope:
[polling state must survive document replacement](https://github.com/avibe-bot/avibe/pull/1640#discussion_r3833946834),
[a failed browser fetch cannot consume manager-owned confirmation](https://github.com/avibe-bot/avibe/pull/1640#discussion_r3835632885),
and [a terminal response cannot trigger another automatic Runtime request](https://github.com/avibe-bot/avibe/pull/1640#discussion_r3835792302).
Their resolution on PR #1640 records the split, not discharge of the behavior:
the successor owns their consuming tests and exact-head close-out together with
the request-owner ruling below.

1. One request owner spans precondition acquisition, transport attempt, and
   publication, is exception-closed, and has one publication point. It uses the
   write-set predicate for identity: source configuration, manifest pin, and
   command fingerprints are included because Avibe does not write them; PID,
   `base_url`, and spawn paths are excluded because Avibe does. Start success
   cannot clear request evidence; only a successful request can.
2. The owner publishes `continuous`, `confirmation_pending`, or `manual_only`.
   The browser owns pacing only and never eligibility. It has no
   `checksRemaining` or other confirmation counter: a rejected poll did not reach
   manager admission and cannot consume the owner's confirmation budget.
3. A bound on automatic attempts never gates user-initiated work. Ordinary page
   loads, assets, and an authorized Retry-now request always attempt transport.
   When automatic confirmation stops, public-viewer copy must name an action that
   viewer can perform: reload the page, then contact the owner if it still fails.
4. Polling state is isolated from document replacement. A classic script may not
   redeclare top-level lexical bindings after `document.open()`; use a private
   scope or a real navigation, and test a non-terminal-to-non-terminal transition.
5. A terminal response is rendered without another Runtime request. Reloading an
   ordinary Show Page GET after `manual_only` would manufacture the automatic
   attempt the owner just refused. Header absence may reload because it proves the
   Runtime is serving; a changed terminal disposition may not re-enter transport.

The successor must preserve the retained transport evidence closure: the UI has
no fallback that fabricates reason, class, disposition, or action. It must also
preserve owner authorization: only the authenticated instance owner may request
an explicit bypass, and a public or limited viewer is never shown an owner-only
action.

**Explicit retry is authorized by the control-plane owner.** The recovery header
expresses a request; it never grants authority. Avibe is a personal, single-user
control plane, so only the authenticated instance owner — the same subject that
could run `vibe runtime prepare` — may bypass automatic bounds and spend local
compute on an install or spawn. Public and limited share-link viewers always
remain automatic, even when they send the header, and the page does not render a
Retry now action they cannot use. This is authorization rather than rate
limiting: an owner pressing the button is the intended unconditional bypass.

Recovery action is another owner-published value, separate from disposition.
There are exactly three user obligations the page can truthfully present: run a
repair, change a setting or prerequisite the owner controls, or accept that no
local action can help. Policy reasons retain the exact setting token and select
the second obligation; unsupported platform selects the third. Every other
failure selects repair unless its owning evidence says otherwise. Adding a case
extends this closed data domain instead of adding a page branch that reinterprets
the nearest reason or class.

**Through one admission path.** This requirement was missing from the first
version of this spec, and its absence produced four review findings across two
heads of PR #1634 — see the note below. "Three opportunities attempt
installation" must not become three implementations. Exactly one manager-owned
method admits an install, and it owns all four of:

1. the opt-out policy (`auto_install`, `VIBE_SHOW_RUNTIME_AUTO_INSTALL`,
   `VIBE_INSTALL_SKIP_SHOW_RUNTIME`);
2. serialization — one lock covering every provider, not just the manifest
   provider's `_install_guard_locked`;
3. the retry classification above;
4. writing the resulting command into shared manager state, so a later caller
   reuses it instead of installing again.

`prepare()`, the startup reconcile, and the request path are callers of that
method. None of them re-implements any of the four, and none of them reaches
`_install_managed_runtime` directly.

A corollary the callers need: a startup reconcile result must distinguish the
*dependency outcome* from *whether prewarm is allowed*. A runtime that was
deliberately skipped is not a failure, but it is also not ready to prewarm, and
one boolean cannot carry both. This falls out of having a single owner; it is not
a separate fix.

**One admission path is not enough; the admission *outcome* also needs one
shape.** PR #1634 consolidated the path and the same class reappeared on the next
head at a different consumer, because `ok` still flattens three independent
states into one boolean. Name them separately and let every consumer read the
dimension it actually cares about:

- **policy** — is installing allowed here at all, and if not, which knob said so.
  `VIBE_SHOW_RUNTIME_AUTO_INSTALL=0` is an *automatic*-install opt-out; an
  explicit `vibe runtime prepare` is a direct user request and must not be
  blocked by it. Flattening these two is why the opt-out silently disabled the
  manual command.
- **install** — installed, absent, or failed, carrying the
  transient/permanent/configured/checksum classification.
- **runtime** — proven serving, not yet checked, or failed to start, carrying its
  own reason.

"Skipped by policy" is not "ready", and neither is "installed". `vibe runtime
prepare --strict` must not report ready for a runtime that is merely not
forbidden. Consumers — the CLI, startup reconcile, the prewarm gate, the `/show/`
request path, the recovery page payload, and `vibe doctor` — project from this
value. None re-derives it, and none invents a local proxy for it.

**The install dimension must be answerable from disk alone.** Today it is not:
`_installed_manifest_runtime_command` (`core/show_runtime.py:1632`) opens with
`_load_runtime_manifest()` and returns `None` when that fails, and
`_load_runtime_manifest` (`:1865`) fails for a remote `manifest_url` both when
`self.offline` is set and when the download raises
(`runtime_manifest_unavailable_offline`, `runtime_manifest_download_failed`). So a
machine that merely cannot reach the manifest host right now reports its
perfectly good on-disk runtime as absent — the original failure this document was
written about, reappearing in the status surface. The coupling is not arbitrary:
install directories are content-addressed on
`sha256(runtime_version:platform:archive_sha256)`, so without the manifest the
code does not know which fingerprint to look for.

`current.json` already answers that question without the network — naming the
last install is the reason it exists. One resolver owns "what is installed on
disk", reading the pointer plus each install's `.vibe-show-runtime.json`, and
fails closed unless the `install_dir` resolves safely under `versions/`, the
provider/platform/source lineage matches, and the pointer agrees with the
metadata on runtime version, platform, and **archive** digest. Not the
whole-manifest digest: W2 deliberately narrowed the install identity to
`sha256(runtime_version:platform:archive_sha256)` so that an edit to some other
platform's manifest entry cannot invalidate a good install, and demanding
manifest-digest equality here would reintroduce exactly that sensitivity. A
manifest digest is validated for shape, never for equality. It establishes
`install = installed` and nothing
more; the runtime dimension stays `unchecked` until something proves the runtime
serves. Policy skip and offline status both project from it. `offline` then means
only "do not fetch", never "assume nothing is installed".

**Policy inputs, decided once.** The first version of this section said an
explicit `vibe runtime prepare` must not be blocked by the auto-install opt-out
and said nothing about the other knobs, which left `force` bypassing everything.
The full space, so no row is left to inference:

| Input | Automatic preparation | Explicit user command |
| --- | --- | --- |
| `VIBE_INSTALL_SKIP_SHOW_RUNTIME=1` | skipped | allowed |
| `VIBE_SHOW_RUNTIME_AUTO_INSTALL=0` | skipped | allowed |
| neither set | allowed | allowed |

`force` is orthogonal to every row: it decides whether an existing install is
replaced, and never grants or removes authorization. Tests cover the whole table,
not the one cell that produced a finding.

`VIBE_INSTALL_SKIP_SHOW_RUNTIME` does not block an explicit command, and that is
a deliberate reading of what it is. It is consulted by `install.sh:572`,
`install.ps1:357`, and `vibe/upgrade.py`, its own message calls it skipping
"preparation", the e2e install and upgrade tests set it to avoid fetching ~21 MiB,
and it appears nowhere in `avibe-docs`. It is a "do not do this on your own"
flag — installer, upgrade, startup, prewarm, request-path auto-install — not a
standing prohibition on the machine ever holding the runtime. Treating it as one
would strand any environment that has it exported with no documented way to lift
it. Both knobs are therefore the same policy class reached through different entry
points; they differ in which automatic paths they cover, not in whether an
explicit request overrides them. If a real "never, not even explicitly" policy is
ever wanted, it needs its own documented knob, and refusing an explicit command
must name the knob that refused and how to lift it.

**Prerequisites are not policy.** A missing `node` is an environment fact, so it
reports as an install/runtime failure carrying `runtime_node_missing`, never as
"skipped by policy" — folding it into the policy dimension would be the same
flattening this section exists to prevent. Declining on a prerequisite is also not
an attempt: it must not set the attempted flag, or the lifetime latch fires and the
gate buys nothing.

**Enumerate the consumers; do not wait to be shown the next one.** This class has
now been found four times — `ok` flattening three states, the recovery poll's page
marker, the doctor verifier's URL marker, and manifest readability standing in for
install state — each time at a consumer the previous round had not touched. Fixing
them one at a time cannot terminate. The closing move is an inventory: list every
reader of policy, install, and runtime state, and state per reader which dimension
it consumes and that it derives none of them locally. A ruling that still leaves a
consumer for the reviewer to discover is not the terminal rule.

**State answers "what is"; an operation answers "what happened."** The fifth
instance of the class arrived from the opposite direction, so it is worth stating
separately. Under a forced repair, when the replacement package fails to download
or parse, the manifest, archive, and github providers all fall back to returning
the previously installed command *and clearing the install reason*. Admission then
reports `ok`, and every consumer of a forced `prepare()` infers from that proxy
that the replacement it asked for completed: `vibe doctor repair` prints repaired,
and the Web UI job at `vibe/api.py:8424` prints "Show Runtime ready." Nothing was
replaced.

No state snapshot can answer this question, because the state is identical whether
replacement succeeded or failed and fell back; only the histories differ. So the
fix is not a fourth dimension and not a new field beside `ok` — a second field a
consumer may forget to read is the same proxy one column over. It is what `ok`
means:

> `prepare()` reports the outcome of the operation it was asked to perform, and
> `force` selects which operation that was: without it, "make the runtime
> available"; with it, "replace the install." `ok` is true only when that
> operation completed.

After a failed forced replacement two things are true at once and both are
reported: the operation failed, and the old runtime is still installed and
serving. That is what the split is for, not a contradiction needing a new
concept. Falling back is a successful state outcome and a failed operation
outcome, so the fallback path must stop erasing the reason. The reuse decision —
"we hold an existing command; may we return it as this admission's success?" — is
one table keyed on whether replacement was required and whether it completed, and
it gets one owner that all four providers call. Three of the four had the same
bug at three call sites, and W3.1 adds more sources to this ladder; a convention
would not survive that.

The single owner must not take "did the replacement happen" as a declared input.
The first attempt at this rule gave the owner the decision and left each provider
to report `replacement_completed`, which is a free parameter and therefore not a
terminal rule. The manifest and archive providers happen to be sound because their
claim sits immediately after `shutil.move`: reaching that line *is* the
replacement, so the claim is a structural consequence of the code position. npm's
is not — it comes from a subprocess that decides for itself whether to do any
work, and `npm install` on a satisfied tree does none, so a corrupt-but-satisfied
package reports a completed replacement after touching nothing.

> `replacement_completed` is sound only where reaching that line is itself the
> replacement. Where replacement is delegated to something that may choose to do
> nothing — a package manager, a build tool, a remote — the code makes it
> unconditional before delegating, so the claim becomes structural again.

npm therefore removes the managed package tree before installing under force,
which is the discipline the github path already follows when it drops the build
marker before `npm ci` so a failure leaves "unknown" rather than a stale truth.
Every call site that passes this argument carries a one-line justification of why
its line is the replacement; "the tool reported success" is the defect, not a
justification. Destroying first creates its own obligation: a path that has
already removed the old artifact converts every failure into a structured
outcome, because raising past the owner leaves the caller unable to distinguish
"nothing happened" from "the old runtime is gone."

**Decide the operation outcome where nothing can get past it.** Three rounds of
this section's rules each inventoried one notch too narrowly — the consumers of an
outcome, then the declarers of the evidence, then the call sites of the owner — and
each asked what goes *through* the structure rather than what gets *past* it. The
path that finally proved it is the explicit-command branch: with
`VIBE_SHOW_RUNTIME_BIN` set, admission returns before `force` is ever read, so a
forced prepare reports success having replaced nothing, and both Doctor and the
Web UI job say the runtime is ready. Eight of the nine exits of
`_attempt_managed_install` handle `force` correctly, including the install-guard
contention branch that deliberately refuses to reuse a command under force. The
ninth was written to answer "is there a command?" and was never revisited when
`force` acquired meaning.

So the decision moves to admission, where every exit already publishes and no path
can leave without doing so. `_attempt_managed_install` returns the state and the
operation outcome as two values with no default on the operation, so a future exit
cannot omit it; the provider-level helper becomes one contributor rather than the
sole owner. State and operation still travel side by side, neither derived from the
other, exactly as the payload keeps `ok` and `status` apart — the operation outcome
does not join `ShowRuntimeAvailability`. The terminal inventory is therefore the
exits of one function, a closed set a reader can enumerate, rather than the callers
of a helper, where "it bypassed the owner" is expressible.

A forced replacement of a user-supplied command is not a failure; it is not
applicable. Those bytes belong to whoever set the knob, so the operation is refused
naming the knob, in the same shape this section already mandates for policy
refusals, and the report says what is true: this runtime is a command you supplied,
and it starts or it does not.

That refusal is named in the operation outcome, never in the state. The first
attempt wrote `policy_reason="VIBE_SHOW_RUNTIME_BIN" if force else None` onto the
published availability, which made the policy dimension a function of what the
caller asked: the same machine reported policy ALLOWED to `status()` and policy
SKIPPED to a forced prepare. It was not cosmetic, because `prepare()` reads that
field to decide whether the status snapshot may touch the network
(`status_offline = True if policy is SKIPPED`), so a forced prepare silently
computed a different snapshot than an unforced one. The gating is right for the two
real knobs and broke only because a force-dependent value landed in the field it
reads — one proxy, two consumers, divergent answers, the same disease this section
exists to close. Hence: **the policy dimension is a function of configuration
alone; if its value can change because the caller asked for something different, it
is carrying operation history.** Whether an explicit command should report as
policy-skipped *unconditionally* is a separate and defensible question, but it
changes what status prints for every such user on every call, so it belongs to its
own change with its own copy rather than arriving as a byproduct.

The same rule, read backwards, governs the destructive direction. `vibe doctor
repair`'s startability check has three outcomes, not two — starts, does not
start, could not be determined — and only the middle one authorizes replacement.
An unproven precondition never authorizes a destructive action, so a machine that
merely failed to create a temp workspace must not have its working runtime
rmtree'd. The flattening was possible because the verifier returned a
`(result | None, error | None)` tuple and each consumer re-derived meaning from
`result is None`: two consumers forty lines apart, one fixed and one left to be
found by the next review, which is the failure this section's consumer-inventory
rule exists to prevent. The verifier returns one named outcome instead, so there
is nothing to re-derive and every consumer branches exhaustively. "Could not
determine" is never reported as `runtime_start_failed`; it carries
`runtime_start_verification_failed` and its detail, because claiming a startup
failure nobody observed is the same lie as claiming a replacement nobody
performed.

**The destructive transition is itself a boundary.** Making replacement structural
moved real destruction into the install path, and the code around it still assumed
nothing is ever removed. The shipped boundary owns cache invalidation and delegate
failures; authorization to move a developer-visible checkout leaves for the successor
section below.

*Invalidate before destroying.* `_managed_command` is a manager-level cache read at
seven sites as proof that installed bytes exist, including `ensure()`'s spawn fast
path and the policy-skip publisher, and nothing cleared it when the new code removed
the artifact it names. A forced replacement whose build then failed left the next
unforced `prepare()` reporting installed and completed with a path to a file that no
longer exists. The provider-local instance of this was already fixed by setting
`existing_command = None` after removing the tree it resolves from; the cache is the
same insight one level up, which is where every fix in this section has ended up.
**A cached path is not evidence of bytes: any state that stands in for the artifact
is invalidated before the artifact is destroyed, not after the replacement is
judged.**

#### Successor: protect the managed GitHub checkout

The baseline this successor must replace is deliberately named rather than implied.
Once the cheap build-marker short-circuit stops matching, master runs
`git checkout FETCH_HEAD` without authorization. A developer with local commits in the
managed checkout can therefore have them displaced by any `/show/` request. Removing
checkout ownership from 2b′ does not introduce that hazard because none of this work
has merged, but it does leave the known pre-existing behavior unfixed. Protecting the
managed checkout is the successor's headline; a three-valued decision type is the
means, not the project.

*Refuse rather than coerce.* Deleting the build output proves the build wrote output;
it says nothing about what the build was fed. `git checkout FETCH_HEAD` keeps local
modifications to tracked files, and at the same revision it changes nothing at all,
so a modified managed checkout can produce bytes attributed to the fetched revision.
The remedy is not to force the tree back — that discards uncommitted work in a
directory a developer may be editing, which is a supported source path, to make our
own evidence claim true. `--force` authorizes replacing the install; it does not
authorize discarding someone's edits, and a user asking for the first is not asking
for the second. So contamination makes the forced replacement *not applicable*,
refused with its own reason, the same shape as the explicit-command knob above — the
third finding in this section resolved by declining an operation rather than
performing a misreported one. The claim then holds in the remaining case: clean tree,
at the fetched revision, output deleted, output resolves. Because a default that
refuses is only safe if the ordinary path is clean, the check is measured against a
normal clone-and-build before it ships, and narrowed to tracked modifications if
build noise makes the broad check misfire. A caller who does want the destructive
restore gets an explicit affordance with its own copy, never a silent widening of
`--force`.

The authorization predicate took two more probes before it closed, and both are worth
recording because the second one killed an appealing fix. A clean worktree can still
hold commits the fetched revision does not contain, so porcelain-empty never meant
"nothing here is the user's." The natural repair — require `HEAD` to be an ancestor of
`FETCH_HEAD` — cannot work here: the managed checkout is created with
`clone --depth 1` and updated with `fetch --depth 1`, so the previous revision's
parents sit behind the shallow graft and `merge-base --is-ancestor` returns false for
the ordinary forward update, refusing exactly the case a forced repair exists to
serve. Measured, not reasoned: a hermetic depth-1 clone at the previous revision with
an empty porcelain returns `rc=1`. **Ancestry is unavailable in this repository by
construction, so the rule cannot be lineage-shaped.** What is available is the same
thing every terminal rule in this document rests on — data our own code wrote:
Avibe records the revision it checked out, and `HEAD` equal to that record proves
nothing was committed on top. A missing record is the genuinely undetermined case and
must not strand checkouts made by older versions.

Scope note, because it decides the remedy: `git checkout FETCH_HEAD` from a clean tree
deletes no commits. They remain reachable through the reflog, and through the branch
ref when `HEAD` was on one; this checkout keeps `HEAD` detached, so committed
work-in-progress becomes reflog-only and gc-eligible after the expiry window —
recoverable for weeks, then gone. The evidence property is untouched either way, since
a build attributed to `FETCH_HEAD` after a checkout is a true claim about
`FETCH_HEAD`. So the obligation is to notice and refuse the forced path, not to
manufacture a proof the repository cannot supply.

Two obligations come with a check whose default answer is refusal. **It is measured
against the real artifact, not against a fixture written to satisfy it** — a test
upstream given exactly the ignore rules the check needs proves the code reads its own
input correctly and says nothing about whether the repository it will run against
looks that way, so the premise is verified by cloning and building the real thing
once and recording the output. And **a refusal whose resolution belongs to the user
names the place and the action**, because it is the one failure in this path that
nobody but the user can clear; reporting `runtime_github_source_dirty` through the
generic prepare-failure copy hands them a machine token where the adjacent
explicit-command refusal hands them the knob's name.

*The guard belongs on the action, not on the mode.* The seventh probe found the
predicate correct and unreachable where it matters. It was consulted under
`if replacement_required and not self._github_source_allows_replacement(...)`, while
`git checkout FETCH_HEAD` ran unconditionally — so the automatic path moved `HEAD`
with no authorization at all, protected only by a cheap-path short-circuit that stops
firing the moment upstream advances past the built revision. A developer with local
commits in the managed checkout loses them to any `/show/` request, which is the harm
the forced path had just been taught to refuse, on the path taken far more often. The
position was right and the condition was inherited from the iteration where the
property was still called "forced replacement authorization." It is not: it is
**Avibe may take over this checkout**, so every path that will move `HEAD` asks it.
The answer differs by path, and refusing is wrong on the automatic one — it would stop
serving the page over a workflow annoyance. Least harm there is to leave the checkout
alone, build what is present, and record that the managed update was skipped, which on
a machine where someone has committed local work is also the less surprising behavior.

Serving an unmanaged checkout is a fourth fact, and the first attempt to report it went
through the flat top-level `reason`, which every consumer reads as "why the runtime is
not available" — Doctor's failed branch, `runtime.prepare.failed`, and a status line
printed whenever the field is non-empty. Measured on a healthy runtime: `Installed:
yes` directly above `Reason: runtime_github_source_revision_changed`, with nothing
anywhere saying the page is being served from the developer's commit. That is 2a's
three-state contract violated one field further out, so the skip belongs in its own
`github_source.update` and `reason` stays empty when nothing failed. Removing the leak
owes the user the signal it was standing in for: `status` states that the runtime is
serving a revision Avibe does not manage and names it, and `prepare` says it prepared
but skipped the managed update instead of printing a bare success. This is the only
surface where anyone finds out, so it is copy, not a token.

**Evidence decides the verdict; the caller's mode may decide the remedy, never the
verdict.** The same commit that moved the guard onto the action also adopted an
unverified revision as the managed baseline when the caller was not automatic — and an
unverified revision is by definition the case where nothing establishes whose commit it
is, so the adoption rests on who asked rather than on what is true. A legacy checkout
holding local commits with a clean tree is indistinguishable from one sitting at an old
upstream tip, and the first has its commits written into Avibe's record, after which
the next forced repair is authorized to check them away. Mode-keyed authorization is
the bug that paragraph fixed, reappearing one branch over, which is why the rule is
stated as a separation rather than as another input: the predicate reads evidence only,
and refuse-versus-skip is the caller's business. A checkout with no evidence is
reported, not adopted; rejoining managed updates is the user's explicit act with its
own copy, like the destructive-restore affordance above. The build marker stays
unwritten on the skipped path, which costs a rebuild per process for that checkout —
accepted, and not to be bought back with a marker that means two things.

A record written after the act it records can be false about our own action, and that is
a different failure from an unproven one. When `checkout FETCH_HEAD` succeeded and
persisting the revision did not, `HEAD` moved while the record stayed behind, so the next
forced repair told the user to move or back up local commits they never made and to
restore a revision they never chose. The harm is that false accusation, not the internal
inconsistency, and it does not need atomicity to fix — it needs the record written
first. Hold two values, the revision Avibe has committed to and the one it intends to
move to; write the pending value before the checkout and refuse if that write fails,
since nothing has moved yet; normalize to a single value after the checkout succeeds.
Authorization is then a clean tree with `HEAD` at either value, and every window heals
forward on the next run: a failed checkout leaves `HEAD` at the committed value, a crash
before normalizing leaves it at the pending one, and both are authorized to proceed.
Nothing is ever rolled back, so this stays a two-valued state rather than a journal and
leaves W6's atomic switch untouched. One residual is accepted rather than handled: a
checkout that dies after writing files but before moving `HEAD` leaves a genuinely dirty
tree, so the dirty refusal fires first and says the directory has local changes, which is
true and whose remedy works.

Reporting splits along the same seam. The update outcome travels in the update record and
nowhere else — a failed or skipped managed update never reports as an install failure,
because the install is whatever is on disk and it is fine. And the record on disk is the
only source of truth: a successful read that finds no file means there is no skip, so the
in-memory copy is a write-through cache that is never consulted as a fallback, which is a
deletion rather than an added invalidation.

Then a mode branch got in front of that owner one more time. A refused takeover under a
forced repair returned the operation failure without writing the record, so the one
disk fact describing the refusal was missing and `vibe runtime status` kept showing an
earlier attempt's target. The four other non-advancing exits in that function already
write before they return, which is the tell: the code treats the record as
unconditional and this branch simply preceded it. So the rule is positional and can be
checked by reading — **record the outcome above the mode branch; the mode branch holds
only the remedy** — and no helper is added, because the exits that already satisfy it
are already in the right shape. The forced return keeps its own operation reason: the
immediate operation did fail and the persisted update outcome was missing, two channels
with two true statements, and collapsing them is the conflation this seam exists to
prevent.

Three findings-bearing heads on a PR that changed a data model is a stop by itself, so
the inventory was diagnosed rather than patched a third time. All three findings share
one root: that function has seventeen exits, and each ruling here adds an invariant
that must hold at every one of them, enforced only by whoever writes the next branch
remembering. That is an enforcement gap, not a wrong record design — none of the three
is evidence against the two-valued record. The remedy is therefore the positional move
plus one parameterized case over every refusal reason in both modes, which turns the
convention into coverage. Its fixture has to start from a source directory that already
carries an older update record with a different target, because the harm named here is
a stale record and a clean directory would let a loose assertion pass on the broken
code. If a fourth head lands another exit-invariant violation, the remedy stops being a
move and becomes one update-outcome owner that cannot be bypassed.

The fourth findings-bearing head exposed two remaining boundaries and closes this
class with two rules whose enforcement is structural rather than conventional.
First, an unknown revision is absence of evidence, not a value that may compare
equal to another unknown. `_git_revision` failure becomes a structured source-update
failure before any checkout-record comparison. Revision comparisons live only on the
checkout-record type, whose authorization methods accept a proven string revision;
callers do not compare `revision` or `pending` fields directly. This makes a normal
stable record with `pending = null` incapable of authorizing an unreadable `HEAD`.

Second, checkout creation and ownership publication are one transition. Avibe clones
into a same-parent staging directory, writes the schema-versioned ownership record
inside that checkout, and only then renames the complete directory to `source_dir`.
Therefore an Avibe-published `source_dir` is never externally reachable without its
ownership evidence. Best-effort staging cleanup is hygiene rather than correctness:
if cloning, revision inspection, record persistence, or publication fails, the final
path was never exposed and the next attempt can retry without adopting or deleting an
unverified checkout. Existing unverified directories remain user-owned evidence and
are not mutated by a healing shortcut.

This is the final in-PR consolidation for checkout evidence and the destructive
boundary. If a fifth findings-bearing head produces another instance of this class,
the two-valued record model itself must be reconsidered; another exit guard is not an
acceptable remedy.

The fifth head fired that tripwire, so the promise is discharged here: the
two-valued record was reconsidered and the evidence exonerates it. No finding
on any of five heads has been about `revision` / `pending` semantics. The
tripwire fired on the wrong axis because it bounded an artifact — "the record"
— when the property at risk was every piece of evidence about the checkout,
wherever it is stored. A governance rule that names an artifact instead of a
property is the same inadequate-proxy defect this document is about, one level
up.

What head five actually showed is cheaper to state: each of its three findings
is a rule already written above, applied at the site a reviewer named rather
than owned by a module. An unowned rule buys exactly one more review round, so
three unowned rules bought this one. The closing rules therefore name owners.
Checkout evidence lives inside the checkout it describes, so a successful
publish retires it by rename and no deletion is a transaction boundary. A
dimension of state has no flat mirror beside its structured payload, so a
consumer cannot reach a location without holding the state that makes the
location meaningful. Artifact provenance is published from the build marker and
cited from nowhere else, so a revision claim cannot be satisfied by checkout
identity. Each remedy removes a mechanism or publishes an owner that already
existed; none adds a branch.

New boundary, stated as a property rather than an artifact: a rule enters this
document with the name of the module that makes violating it impossible. If a
sixth head returns either a rule with no named owner or a payload field whose
meaning depends on a sibling field, the update record and the status payload
leave this PR and become their own change.

The sixth head fired that boundary. The persisted update record used
`state = "skipped"` for both a known target that checkout takeover refused and
a fetch or revision-inspection failure whose target might not exist. A consumer
could interpret that field only by pairing it with `reason` and the nullable
`target_revision`, so the state was another inadequate proxy. The record, its
`github_source.update` status member, and the prepare/status copy leave this PR
rather than acquiring a failure-specific consumer branch.

The install dimension stays. Its location is a different member of the status
payload, its flat mirrors were removed on head five, and no later finding has
implicated its owner or consumers. The checkout record, build-marker
provenance, and operation outcome contract stay for the same reason: the sixth
finding disproves none of them. This is a split by the property that recurred,
not a rollback of adjacent owners that have held.

The follow-up starts from the reason the update record existed, not from its
schema. Doctor can already report a failed managed update from the immediate
operation reason, but an ordinary prepare may still succeed by reusing the old
runtime and therefore clears that top-level failure. The record persisted that
degraded side effect so a later status call could repeat it. That past event is
not current state: `vibe runtime status` performs no network I/O and cannot know
whether an update is available. The leading design is therefore for the
operation to report its degraded side effect in band to the caller that just
performed the fetch, while status makes no update claim it cannot verify. The
follow-up may depart from that design only with a stated reason.

The next boundary is again a property: if a seventh findings-bearing head
returns a finding against one of the remaining owners -- checkout evidence,
the install dimension, build provenance, or the operation outcome contract --
that owner leaves this PR as its own change. Another site-level patch is not an
acceptable remedy.

The reason this one predicate absorbed seven probes is worth naming even though it is
out of scope here: one directory is both Avibe's build input and a place a developer
may work, so every rule about it has to infer which role it is in. An eighth input
would be another inference. If the predicate needs narrowing again, the answer is to
stop conflating the two roles, not to add a term.

The seventh findings-bearing head fired that boundary, but on a different axis than
the artifact and owner boundaries above. One two-valued type repeatedly carried a
three-valued reality, and every consumer promoted "cannot determine" to one of the two
definite answers:

| Type | Two represented values | The third value it could not represent |
| --- | --- | --- |
| `ok: bool` | available / unavailable | the requested operation did not run |
| raw `revision` / `pending` comparison | equal / different | neither revision is known |
| update `state = "skipped"` | skipped / absent | fetch never produced a target |
| takeover `allowed: bool` | permitted / refused | ownership could not be determined |
| `install_dir` beside `installed` | present / absent | intended, not achieved |
| `current_revision` used as provenance | a revision / none | checked out but never built |

The governance boundary named the wrong axis twice. Head five bounded the update
record when the property covered every piece of checkout evidence; head six bounded
owners when the recurring risk lived in the two-valued type those owners shared. The
seventh finding made the consequence concrete: a failed legacy ownership-record write
returned `allowed = false`; the automatic consumer treated that as developer-owned,
skipped the checkout, cleared the released build marker, and used the same boolean to
decide not to restore it. One boolean gated both moving the checkout and destroying
build evidence even though those actions have different safety conditions.

That gate and its ownership-record subgraph leave 2b′. The finding disappears with
the gate: the retained path returns to master's unconditional build-marker restoration,
so there is no site-level branch to patch. The successor takes the takeover decision,
its consumers, and ownership-record transactionality. It starts with a named outcome
of **proven / refused / undetermined** and structurally separates "may move this
checkout" from "may destroy build evidence". Undetermined answers no to both. A write
failure while adopting legacy evidence therefore returns the existing command
untouched, preserves the marker, and recovers when permissions recover. Departing from
that shape requires a stated reason.

2b′ keeps the install dimension, build-marker provenance, the operation outcome
contract, provider replacement evidence, destructive cache invalidation and delegate
failure normalization, and Doctor's first-install/reinstall copy split. These owners
have drawn no finding for two heads, and 2c depends on the operation contract rather
than checkout ownership. The successor may consume the shipped `_github_install_attempt`,
generic update-failure reason, `_git_revision`, and `_read_github_build_marker`; those
are one-way dependencies on retained contracts, not checkout ownership leaking back
into 2b′.

#### Shipped in 2b′

*One execution boundary for delegated work.* The shared install-command helper
already passed a five-minute timeout and let `TimeoutExpired` escape — someone
decided a hung subprocess must not block forever and never decided what that means
to the caller. Nonzero exit, spawn failure, and timeout are one thing to admission:
the delegate did not produce a runtime. They collapse at the shared boundary, so
every call site inherits it and no exception crosses admission from a path that has
already destroyed state.

Note for expectation-setting: this fixes transient failures only. It would not
have helped a user who cannot reach `github.com` at all; that is W3.

**Acceptance:** after a simulated download failure, a later `/show/` request
retries once the backoff expires and succeeds, with no service restart. A forced
repair whose replacement package fails reports the repair as failed with its real
reason, reports the old runtime as still installed, and leaves it on disk; a
startability check that cannot reach a verdict refuses the reinstall instead of
performing it, and reports that it could not tell rather than that startup failed.
A forced repair under the npm source does not report a completed replacement when
the package tree was left untouched, and one whose install raises after the old
tree was removed reports a structured failure rather than escaping. A forced repair
of a runtime supplied through `VIBE_SHOW_RUNTIME_BIN` is refused naming that knob
instead of reported as reinstalled. After a forced replacement whose build fails, a
following unforced `prepare()` reports the runtime as absent rather than resolving a
cached path to bytes that were removed; a forced repair against a locally modified
managed checkout is refused naming the modification instead of overwriting it; and a
delegated install that is killed by its own timeout is reported as a provider
failure rather than escaping to the caller. And with upstream advanced past the
built revision, an automatic `/show/` request against a managed checkout carrying
local commits leaves `HEAD` where it is and serves what is on disk, reporting the
managed update as skipped in its own field and naming the served revision in
user-facing output while the availability reason stays empty; and a checkout whose
revision Avibe cannot verify is still refused after an unforced `prepare` rather than
adopted as the managed baseline.

### W5 — Tell the truth on failure (repo: `avibe`)

- Thread the manager's `reason` into `show_page_runtime_recovery_html` and branch
  the copy on it. For an infrastructure failure, drop the `src/App.tsx` prompt and
  surface `vibe doctor repair show-runtime`.
- Raise the `/show/` failure logging from `logger.debug` to `logger.warning` and
  include the reason code.
- Split `SHOW_RUNTIME_RECOVERY_LOADING_DELAY_SECONDS` into two constants: the
  page's loading-state delay and the node CLI's `--fallback-delay-seconds`.
- Render a terminal failure immediately instead of after the 30 s delay.
- Poll so the page self-recovers once the runtime installs, without a manual
  reload.

Recovery copy projects the user's obligation separately from the retry
disposition. A repair command is shown only when local repair can address the
failure; a deliberate policy opt-out names the setting to change; and an
unsupported platform states that no local repair or retry changes the fact. The
page never maps failure class to retry behavior. Install, runtime, and policy
publish their disposition and recovery action inside their own dimensions, with
no flat top-level mirror for a consumer to mispair.

All user-visible strings go through `vibe/i18n/`.

**Prove readiness; do not accept a proxy for it.** Two findings on PR #1634's
third head were the same mistake in different places, and both are worth stating
as a rule because the reduced signal looked adequate each time.

- `ensure()` returns `ShowRuntimeResult(True, base_url)` as soon as
  `_read_startup_url()` yields a URL (`core/show_runtime.py:306-311`), with no
  health check — while the already-running path one screen above does call
  `_healthy()` before reporting available (`:264`). A runtime that prints its URL
  and immediately exits is therefore reported as available. Every consumer
  inherits this, including `vibe doctor`'s repair verifier, which is what made it
  a finding here. A freshly started runtime must answer a request before it
  counts as serving.
- Automatic recovery is deferred to PR 2c′. Until then the recovery page offers a
  manual reload plus an authenticated owner-only Retry-now action. PR 2c′ may poll
  only from manager-owned disposition, never from a browser counter, and a failed
  poll consumes no confirmation because manager admission never happened.

**A probe proves only the state it was calibrated for.** The first attempt at the
fix above reused `_healthy()` exactly as the already-running path calls it: one
request, `connect=0.5s`, `read=2.0s`, no retry. That budget was chosen for a
runtime that has been serving for a while. Spending it on a process that started
milliseconds ago is the same reduction one level down — the check is now in the
right place and still is not evidence for the state it is asked about.

The asymmetry inside `ensure()` is the tell: `_read_startup_url` polls to a
deadline because startup timing is unpredictable, and the readiness probe beside
it got a single attempt. The cost of a false negative here is not a wrong status
line — the branch calls `self.stop()`, so a runtime that was coming up fine is
killed and the next request repeats the cycle. That is a self-inflicted startup
loop on exactly the slow machines least able to absorb it, traded for the false
positive being removed.

So: **spawn-to-serving is one budget with one knob.** The URL wait and the health
wait share a single deadline rather than each owning one; two deadlines would
double the worst case a page load waits through and invite the question of why
they differ. Within that budget the runtime is proven by polling — retry until
health answers, `process.poll()` goes non-None (fail immediately; catching this
is the whole point), or the shared deadline expires. Each probe is bounded by the
remaining budget, never by a constant of its own. If the total turns out to be
too small for a cold start, it was already too small for the URL wait alone and
gets raised once, in one place.

The current 10 s total budget is calibrated against a stronger runtime contract,
not against Vite startup: the wrapper binds its HTTP listener before printing
`Vibe Show Runtime listening at ...`, and handles `GET /health` directly before
any session or Vite path. At `vibe-show-runtime` revision `f40ac354e`, a real
spawn in a fresh workspace with no Vite cache printed the URL in 167.2 ms and
answered `/health` in 171.3 ms on Node 22.18.0. The 30 s recovery-page fallback
delay is a separate UI budget and is not on this readiness path. W1 must preserve
the direct health contract; if a future bundle makes `/health` depend on Vite,
the single startup budget must be remeasured and raised in the same change.

Distinguish the outcomes at the source: startup URL never appeared, process
absent or exited, health never answered within the budget. All three are
transient start failures and may share user-facing copy, but the codes stay
distinct — this is the only place that information exists, and W4's retry work
cannot reconstruct a distinction discarded here.

**An identity travels with the thing it identifies; consumers project, never
pair.** `dependencies_status()` built the Settings row by taking `installed` from
disk and `version` from the selected manifest, so a runtime installed at A while
the wheel selects B renders as "B installed and ready" — a version that is on no
disk anywhere. This is the same defect as the two above, arriving through a
different door: not a proxy substituted for evidence, but two dimensions' answers
stapled together by the consumer. The manager already owns the comparison
(`status()` returns `installed_matches_manifest`), so the fix is that the
installed identity ships inside the install dimension that owns it and the
consumer reads rather than computes. Not as a top-level sibling of `installed`:
a sibling is two adjacent keys any future consumer can pair the same wrong way,
which is the hazard rather than the fix.

The reporting shape is settled by W4's ruling on the skipped GitHub update: a
stale runtime works, so availability stays true, the displayed version is the
installed one, and the pending update gets its own field and its own copy. The
dependency schema already carries that vocabulary — `version` /
`latest_version` / `has_update`, used correctly by the `askill` entry — so this
is a reuse, not a new field.

One trap in that reuse. `installed_matches_manifest` is only ever set in the
manifest branch; for the `archive`, `github`, and `npm` sources it is
unconditionally false. Deriving `has_update` from it would announce a pending
update on every GitHub-source dev checkout. The boolean is itself a two-valued
proxy for a three-valued state — matches, differs, not comparable — which is
this document's own root cause one level down, so it has to be able to say
"not comparable". And the consumer must not recompute the answer by comparing
version strings: `_manifest_install_matches` compares platform, digest, and
candidate layout as well, so a string comparison would quietly disagree with the
manager it is reporting for.

The same mispairing sits in `core/managed_runtime.py`'s shared `status()`
(`"installed": binary is not None` beside `"version": manifest.runtime_version`),
inherited by the git, memory, and model-hub runtimes, and is repeated downstream
in the `memory-runtime` dependency row. Show Runtime has its own status
implementation and can be correct without touching that base, so those sites are
recorded against the codebase-wide audit rather than pulled into this PR.

**Acceptance:** with the runtime unavailable, the page states the real reason
within a second, updates when the reason changes, and recovers on its own after a
successful repair. A runtime that starts and immediately exits is reported as
failed, never as ready. A runtime that is slow but healthy within the startup
budget is reported as ready, and is never killed for being slow. A runtime
installed at one version while the wheel selects another reports the installed
version with an explicit pending update, and no surface ever renders a version
that is not on disk as installed.

### W6 — Make the switch atomic (repo: `avibe`)

Obtain and verify the new archive before the new identity becomes authoritative,
so a failed update leaves the working install in service rather than abandoning
it. Sequence after W1–W3 so it is not compensating for churn that no longer
exists.

**W6 also owns a second, sharper case, found while reviewing PR #1634.** Install
and install are serialized — `_install_guard_locked` is a `threading.RLock` plus
a `MigrationFileLock`, so it holds across processes, including a separately
invoked `vibe doctor`. Install and *running runtime* are not. The archive is
staged into a temp directory, but the final swap is
`shutil.rmtree(install_dir)` then `shutil.move(tmp_dir, install_dir)`
(`core/show_runtime.py:1850` and `:2148`), and the install directory is
content-addressed on
`sha256(runtime_version:platform:archive_sha256)`. A *forced* reinstall of the
same runtime version and platform therefore resolves to the same path and
deletes the directory a live runtime process is serving from. Identical-identity
forced reinstalls are the exposure, and the `archive`, `github`, and `npm`
providers modify in place as well.

A later review of PR #1634 reached the same exposure through a second door and
made two corrections to the paragraph above. The first: it is not only a *running*
process that loses its bytes. `_resolve_managed_availability` returns a cached
`_managed_command` before it ever enters the install owner, and the same shape
repeats at every cached and disk-reuse exit for all four sources, so a request can
select a command and be about to spawn it while the forced path deletes the
directory that command names. The second correction is that distinct-identity
installs are **not** safe either, once pruning is counted: the `prebuilt-*` /
`manifest-*` glob removal and `_clean_manifest_install_dirs` can delete an older
versioned directory a cached command still points at, and `keep_previous` bounds
retention by count rather than by reference. The atomic swap alone therefore does
not close this; W6 owes **retention bounded by reference**.

The obvious remedy — extend the install lock to cover command selection — was
considered and rejected, because no sound boundary for it exists. A critical
section that ends at spawn does not protect the process: the runtime `require()`s
modules lazily for its entire lifetime, so the bytes it has not read yet can still
be deleted underneath it. A critical section that covers the runtime's lifetime
over-serializes every request behind a long-lived server. Every choice of boundary
is either unsound or unacceptable, and a rule whose free parameter an adversary can
keep probing is not the terminal rule. The terminal rule removes the destruction
instead: **replacement creates and never overwrites, and a path that has been handed
out to a command is never destroyed while anything can still reference it.** Mutual
exclusion between use and replacement then becomes unnecessary rather than
carefully bounded, because the two never address the same bytes. Install-versus-install
serialization is untouched: it remains necessary and sufficient for what it owns.

Until W6 lands, the mitigation is that no caller forces a reinstall without
evidence the install is broken: `vibe doctor repair show-runtime` verifies
startability first — in an isolated temp workspace and runtime, so the check
itself cannot disturb the service — and reinstalls only when that check fails.
W6 removes the reliance by making the swap atomic, at which point the gate is an
optimization rather than a safety property. Any new forced-reinstall caller added
before W6 must carry the same gate.

That mitigation is currently claimed rather than held, which is worse than not
claiming it. The Settings dependencies page has an Install button for
`show-runtime`, and it reaches `prepare(force=True)` through
`_prepare_show_runtime_job` with no startability check at all — a user with a
perfectly healthy runtime can delete the directory it is serving from by pressing
it. The motive was manufactured one thread earlier: while that row paired the
selected manifest's version with a disk install at a different version, it read as
"B installed" on a machine running A, which is exactly what makes someone press
Install on something that works. The two findings are one incident seen from both
ends, and fixing the row removes the motive without removing the exposure. The
remedy is not a second copy of Doctor's gate — one verified-repair path owned in
one place, reached by both Doctor and Settings, so that a forced-reinstall caller
cannot be written without the gate. That is a consolidation and belongs with W6
rather than bolted onto a merge-ready PR; it blocks W6, not the merge, because the
exposure predates these PRs and shipping the truthful row strictly reduces it.

## 8. PR split

| PR | Scope | Repo | Notes |
| --- | --- | --- | --- |
| 1 | W2 | avibe | Small, surgical, unit-testable. Start here. |
| 2a | W4 admission outcome | avibe | The policy/install/runtime contract and every consumer projecting from it. |
| 2b | W5 readiness proof | avibe | `ensure()` health-probes a fresh start; `doctor` inherits it. Independent of 2a. Bounded retry plus Doctor's three-state startability; claims nothing about replacement. |
| 2b′ | W4 replacement operation contract | avibe | What `prepare()` claims, what a provider must prove, install-state ownership, build provenance, and destructive cache/delegate boundaries. Split out of 2b after six findings-bearing heads; checkout ownership is not shipped here. |
| 2b″ | Protect the managed GitHub checkout | avibe | Replace master's unconditional checkout takeover with a three-valued decision that keeps undetermined ownership away from both checkout movement and build-evidence destruction. |
| 2c | W4 install/start retry + W5 page | avibe | Independent install/start retry ownership, atomic admission, typed transport evidence, and an honest manual recovery page. Automatic request recovery is explicitly absent. |
| 2c′ | Automatic request-level recovery | avibe | Reintroduce the browser poll loop together with the request retry owner that bounds it, preserving user-initiated requests and the inherited browser constraints. |
| 3 | W1 | vibe-show-runtime | Independent; effect appears at the next runtime release. |
| 4 | W3.1 | avibe | Source ladder. Valuable on its own — removes the single point of failure using only tiers that exist today. |
| 5 | W3.0 | vibe-show-runtime | Version scheme. Prerequisite for 6 and 7. Gate on owner confirmation. |
| 6 | W3.2 | vibe-show-runtime + avibe | New distribution and the pinned dependency. Release engineering. |
| 7 | W3.3 | vibe-show-runtime + avibe | npm tier and its mirror. |
| 8 | W3.4 | avibe + avibe-docs | Docs; can land alongside 6. |
| 9 | W6 | avibe | After all of the above. |

PRs 1–4 need no new published artifact and no owner decision beyond this plan.
The gate is before PR 5, because W3.0 fixes a version scheme into two public
registries and is expensive to change afterwards.

PR 2 was originally one PR carrying all of W4 and W5. It produced three
findings-bearing heads without a clean pass, and each round returned findings in
the same three classes — admission outcome, readiness proof, recovery reporting —
at a consumer the previous round had not touched. The recurrence was a symptom of
the unit, not of the patches: a PR that owns three cross-cutting properties can
always be shown a fourth consumer of whichever one is still reduced to a proxy.
Hence the split above, one property per PR, each reviewable in a single pass. This
is a lesson about sizing a PR by property count rather than by line count.

2b then split a second time, for a different reason worth distinguishing. It was not
carrying two properties: every finding after the first two belonged to one property,
the operation contract. What it was carrying was a property whose *specification was
still moving*. Six findings-bearing heads each returned the same class at a boundary
the previous round had not reached — providers, then the shared helper, then admission,
then the destructive transition, then the authorization predicate twice — and each fix
was correct at the level it was made. The signal is not "this PR owns too much" but
"this contract is not finished being discovered," and the remedy is different: finished
work stops waiting on it. Hence the readiness proof lands alone and the contract
continues in 2b′.

2b′ then tripped the breaker on its own first implementation head, and that one did
*not* split, which is the distinction the previous paragraph earns. Five findings landed
at once, and none of them narrowed the predicate: one was a deletion restoring an
evidence rule an earlier head already had, one was transactionality, and three were the
same reporting seam. A predicate that is being *unwound* rather than *narrowed* is
finished being discovered, so the remedy was consolidation inside the PR — one owner for
the reporting seam, one for the checkout transition — not another handoff. The
terminating condition was recorded with the decision: the predicate takes no new inputs
and the record takes no new fields, and if either needs one more, the answer is the role
separation and it becomes its own PR. A breaker trip asks which of those two the evidence
supports; it does not answer.

The orchestration rule that produced this, recorded because it was nearly reasoned
away: the trigger was registered while three PRs already sat unmerged, so the argument
that a split frees nothing while the queue is stalled was already known when the
commitment was made and could not later justify overriding it. A pre-commitment that
yields to a fact available at the time it was made is not a rule.

## 9. Verification

**Hermetic unit tests** under `tests/`, extending
`tests/test_show_runtime_*.py` and `tests/test_ui_show_pages.py`. Every test
redirects the entire call path to test-owned state; none may read or write the
real `~/.avibe`.

- W2: platform-scoped manifest edit does not invalidate other platforms;
  legacy-fingerprint install is adopted; GC still protects the adopted install
  and its archive.
- W3.1: ladder order is honored; every candidate verifies `size` and `sha256`
  before use; a corrupt or unreachable candidate falls through to the next rather
  than failing the install; the winning source is recorded in install metadata;
  the reason code only propagates once every candidate has failed.
- W3.2: the packaged archive is preferred over any network tier; an absent
  `avibe_show_runtime` import and a missing archive inside it are both ordinary
  fallthroughs; a size/sha256 mismatch falls through; the `avibe-os` pin matches
  the manifest's recorded distribution version.
- W3.3: the npm tarball's outer sha256 is *not* used for verification; the inner
  archive is verified after extraction; the mirror base URL is used when the
  primary registry is unreachable.
- W4: owner-published failure classification; independent install/start backoff
  schedules; exactly one confirmation for unclassified evidence; prerequisite
  identity changes and explicit intent invalidate a gate without conflating a
  failed replacement with runtime usability.
- W5: reason-specific copy; terminal failures render immediately; i18n coverage.

**Pipeline tests**: extend `tests/test_show_runtime_manifest_packaging.py` with
the pin-versus-manifest consistency check. The existing assertion that the
`avibe-os` wheel carries no archives stays as written. Per-platform wheel content
assertions belong in the runtime project.

**Local Incus regression** (local only, per `AGENTS.md` §3): cut network,
restart, and expect an honest, specific error rather than a spinner. Restore
network and refresh *without* restarting; expect automatic recovery. Then block
`github.com` only, leaving npm reachable, and expect a cold install to succeed via
tier 3. Finally, install with the runtime distribution present and block all
egress afterwards; expect a working Show Page with no download.

## 10. Risks and open questions

- **PyPI quota.** No increase needed (§6). Both distributions stay inside the
  default 10 GiB, and the largest file is ~22 MiB against a 100 MiB limit.
  Re-measure after the first three runtime releases and request an increase from
  real usage if the estimate proves low.
- **glibc floor for the manylinux tag.** Determined by the bundled native
  binaries; must be measured, not assumed. Now scoped to the runtime
  distribution rather than to `avibe`'s build hook.
- **Mirror sync lag.** Tsinghua and Aliyun mirror PyPI on a schedule, so a
  freshly published runtime version can be briefly unresolvable for mirror-only
  users. Tiers 3 and 4 cover the window; publishing the runtime version *before*
  the `avibe-os` release that pins it shrinks it. This is the same ordering rule
  `AGENTS.md` §9 already imposes on manifest assets.
- **Version scheme is expensive to change.** W3.0 fixes a version string into two
  public registries. This is the reason PR 5 is the gated one.
- **Two distributions must stay consistent.** A published `avibe-os` whose pin
  names an unpublished runtime version is broken for every fresh install. The CI
  pin check catches the mismatch at build time; release ordering must publish the
  runtime first.
- **Pre-release channel convention.** `AGENTS.md` §9 describes GitHub-only
  pre-releases, but `3.0.13rc1` is on PyPI. Reconcile the stated convention with
  actual practice while editing §9 in W3.4 — under W3 a pre-release must be able
  to resolve its pinned runtime, so which index it publishes to now matters.
- **Cross-OS determinism in W1.** Windows path separators and case handling are
  the likely sources of residual nondeterminism; the CI double-build check is the
  guard.
- **Non-default sources still float.** `_RUNTIME_GITHUB_REF = "main"` and
  `releases/latest/download` remain unpinned. Not addressed here; worth a
  follow-up so all sources share the pinned semantics.
- **Manifest mirrors.** W3.1 and W3.3 express the ladder and its mirrors in code,
  which needs no change to the manifest schema shared by all five managed
  runtimes. Adding a `mirrors: []` array to that schema (already
  `schema_version: 1`) stays deferred, not rejected — it is the mechanism a
  self-hosted origin tier (§5) would use if one is ever wanted, and it is safe
  because archives are sha256-verified.
- **Generalizing to the other managed runtimes.** `git` and `tmux` share the
  manifest pattern and the same single-source exposure, and together add only
  ~2.5 MiB per platform. Deliberately out of scope here to keep the first
  distribution small; the mechanism generalizes and this is worth a follow-up.
