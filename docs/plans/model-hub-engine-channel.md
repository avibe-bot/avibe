# Independent Model Hub engine channel

**Status:** design approved by the owner, with minimum-only compatibility;
target Avibe **3.2.6**. Delivery is four PRs (section 6), each needing owner
merge approval. Step 1 shipped as avault 0.1.10. Step 2, the channel contract
and its producer, is described under "Step 2 as built" at the end. Steps 3 and
4 are not built: **no installed Avibe adopts an engine from the channel, and
the channel is not commissioned.** This note authorizes no publication, key
creation, or rollout.

## Outcome and current gap

Example: publish `model-hub-engine-v8.0.63-2` with minimum Avibe `3.2.6`.
An installed 3.2.6 or later discovers it without an Avibe release, verifies it,
and changes only CPA at a safe boundary. An older Avibe stays on its own engine.
A failed activation restores the previous working engine. A withdrawn release
cannot be selected again. This is an example, not approval of that engine build.

Independent **artifacts already exist**: the builder produces six deterministic
archives and the guard publishes `model-hub-engine-v…` releases. What is missing
is independent, authenticated **adoption**. Today the installer, Dependencies,
startup reconciliation, and hosted-tool evidence all use the packaged pin.
The publish workflow does not depend on full Model Hub E2E. The November 10
v8.0.63 adoption (#3460, followed by E2E expectation fixes in #3464) demonstrated
why a successful build/hosted-wire check is not full release qualification.

Sources: [CPA lifecycle](model-hub-cpa-dependency.md), [release workflow](../release.md),
[download mirror](release-download-mirror.md), `scripts/build_model_hub_engine.py`,
`scripts/model_hub_engine_release_guard.py`, `scripts/model_hub_hosted_wire_gate.py`,
`vibe/model_hub_runtime/{installer,adapter,supervisor}.py`, `core/managed_runtime.py`,
`vibe/api.py`, and `SettingsDependenciesPage.tsx`.

## 1. One index, minimum compatibility, pinned bootstrap

Add one authoritative discovery object:

```text
https://dl.avibe.bot/index/model-hub-engine.json
```

It is separate from the generic, unsigned `index/releases.json`. There is no
GitHub-latest discovery, second channel, or per-client hosted service. The object
is a signed envelope containing an exact UTF-8 JSON payload (base64 in transit):

```json
{
  "schema_version": 1,
  "key_id": "model-hub-channel-2026",
  "payload": "<base64 of exact payload bytes>",
  "signature": "<hex DER signature>"
}
```

Decoded payload (the manifest is carried byte-for-byte, not reserialized):

```json
{
  "repository": "avibe-bot/avibe",
  "channel": "stable",
  "sequence": 42,
  "issued_at": "2026-11-10T06:00:00Z",
  "expires_at": "2026-11-17T06:00:00Z",
  "releases": [{
    "min_avibe_version": "3.2.6",
    "manifest_sha256": "<64 hex>",
    "manifest_base64": "<exact model-hub-engine-manifest.json bytes>"
  }],
  "revoked": [{
    "asset_release_tag": "model-hub-engine-v8.0.63-1",
    "rollback_to": "model-hub-engine-v8.0.30-2",
    "reason": "<public, credential-free explanation>"
  }]
}
```

Each decoded manifest retains the existing `version`, `asset_release_tag`, source
commit, toolchain/patch identity and six assets (`platform`, canonical owned `url`,
`size_bytes`, archive `sha256`, `binary_sha256`, `bin_path`). Reuse that schema and
strict release guard. The signed digest binds the exact decoded bytes, the CI
receipt and the published manifest; never regenerate the file from parsed JSON.
The placeholders above are not a publishable release.

Each entry adds a required stable `min_avibe_version`; an identity's bytes and
minimum never change after promotion. Corrections require a new revision.
Referenced rollback releases must also be present with complete verified manifests.

Selection rules:

- Compare Avibe with `packaging.version.Version`, not strings. Stable minimums
  exclude an earlier prerelease (`3.2.6rc1 < 3.2.6`). RC validation injects the
  candidate through the test harness, not a production compatibility bypass.
  Unknown/local source versions do not auto-adopt channel engines.
- Filter by minimum, host platform, revocations, and local failed-candidate
  quarantine. Choose the greatest numeric `(upstream major, minor, patch,
  Avibe build revision)`; dates and the upstream version alone are insufficient.
- Ordinary adoption only moves forward from the effective installed engine and
  never below the bundled pin. A fresh index with no newer eligible entry keeps
  the current engine; disappearance is **not** a rollback instruction.
- No maximum or engine-contract version in v1. Compatibility is established by
  candidate qualification against actual Avibe artifacts and persisted-state
  round trips, not by a signature or a second publisher-assigned number. There is
  no independent CPA contract-epoch handshake today. A falsely assigned epoch
  would not protect against a falsely assigned minimum. Future Avibe releases
  must preserve the qualified boundary or revise this protocol before breaking
  it; incompatible state migrations are ineligible for this channel. Raising the
  minimum on a *new* engine leaves older Avibe on their newest eligible entry.
- Every Avibe release keeps its reviewed packaged manifest. Startup, repair,
  `runtime prepare`, demand-start, and cleanup must all resolve the same effective
  target, rather than reinstalling that packaged pin over a newer channel engine.
  Avibe downgrade re-evaluates compatibility and uses its compatible pinned or
  retained engine; it never starts an engine requiring a newer Avibe.
- “Offline default” means the packaged manifest plus a previously verified cached
  archive/install (or preseeded distribution), as today. A bare wheel with no CPA
  cache cannot install CPA offline; bundling six binaries is not part of this work.

## 2. Integrity chain — a hash in an unsigned index is insufficient

Today's trust anchor is the manifest shipped in Avibe, **not** the mirror.
Allowing the mirror to change both the download and its expected hash would
weaken that guarantee. Require:

```text
installed Avibe public verification root
  -> authenticated channel payload (including minimums and revocations)
  -> existing manifest archive size + SHA-256
  -> safe extraction + binary SHA-256 + executable/version check
  -> candidate health and contract check
```

Use a dedicated, non-wallet secp256k1 channel key and the existing avault DER
signing scheme over `SHA256("avibe:model-hub-engine-channel:v1\n" || payload)`.
The algorithm/domain are fixed by the client, not selected by untrusted metadata.
The release signer holds the key inside avault; Python and Agents receive only a
signature. Add a small **keyless, noninteractive verification** verb to avault,
using its existing crypto library; no user Vault unlock or Keychain access is
needed to verify public metadata. This is an explicit cross-repository dependency,
not a claim that avault already exposes this verifier. Update avault's DESIGN
and the Avibe custody integration plan together; no new Python crypto stack.
Existing managed-runtime archive hashing remains unchanged.

Ship the public root and an initial sequence floor with Avibe. Persist the highest
accepted sequence and payload digest; reject older sequences, changed bytes at
an equal sequence, unknown keys/schema, wrong repository/channel, duplicates,
oversized payloads, invalid times or signatures. Accept a repeated identical
payload only while fresh. Seven-day expiry bounds a first-contact freeze; refresh
the signed snapshot daily without changing release eligibility. Clock anomalies
or a failed refresh stop new adoption, not a previously verified working engine.
Local anti-replay state is not a defense against an attacker controlling the OS
account. A compromised signing authority remains a release compromise.

A trusted release job owns promotion/revocation and sequence allocation; one
serialized writer uses conditional object replacement so a stale publication
cannot overwrite a newer revocation. Daily renewal must use the current desired
state, not regenerate eligibility from GitHub release presence. The signing
service/environment is separate from the R2 mirror credential. Root rotation
ships through an Avibe release with an overlap key; no online root delegation in
v1. Signing-key provisioning and protected publication credentials require a
separate authorized operational step before channel enablement.

Keep canonical Avibe-owned asset URLs and the existing mirror-first downloader.
The GitHub fallback is permitted **only for exactly the same hash-pinned asset**;
never accept a digest discovered from that fallback. Restrict repository/path,
redirect policy, archive members, sizes and extraction budgets. A mirror miss,
expired index, or invalid signature cannot authorize unsigned discovery. Keep
accepted envelopes with retained engine manifests so offline restart/rollback
can verify provenance; metadata expiry forbids new remote adoption, not restart
or recovery of a previously verified local generation.

## 3. One update owner and recoverable activation

Extend the controller-owned adapter/supervisor lifecycle and existing cross-process
managed-runtime lock. UI, CLI, periodic checks, startup repair and cleanup converge
on it through IPC. A starting/unreachable controller still owns the engine; never
fall back to a competing UI-side installer. **Without a controller, callers may
stage verified files only:** no pointer change, process start or activation. They
report `staged / waiting_for_controller`, not applied; starting Avibe is not an
implicit side effect. Controller startup acquires the same mutation fence,
recovers pending transactions and selects a verified generation before spawning
CPA. Initial prepare/repair follows this ownership rule too. Network, hashes,
filesystem work and process waits stay off the controller loop.

Automatic checks run after startup and every **15 minutes ±20% jitter**, with
bounded network backoff, even when Model Hub execution is disabled. They do not
block Avibe startup. GET status is a local
snapshot; “Check” refreshes metadata explicitly. Persist last check, active and
previous identities, update result, quarantines and a small transaction journal
under the existing engine runtime directory. Keep these separate from the released
install-claim schema unless an additive migration is actually needed.

| State | Transition / invariant |
| --- | --- |
| `idle -> checking` | Authenticate index and select compatible candidate; active engine keeps serving. |
| `downloading -> staged` | Hold one update reservation; download/verify into a distinct immutable generation. Do not move `current.json` or hold the routing lock during download. |
| `preflight` | Run candidate with isolated temporary config/auth and loopback fixtures; validate startup, management ownership/config and gateway contract without provider traffic or user credentials. |
| `waiting_for_idle` | Wait for a quiet boundary. Under the routing lock, count all inference transports **and** OAuth/management/config operations. Briefly gate new admissions and drain existing work; after 30 seconds reopen admission and defer five minutes instead of killing a stream. |
| `activating` | Journal old/candidate identities and run intent durably; stop only the owned old CPA, select candidate, start if previously running, validate the real local health/ownership/config contract within a 30-second budget. No new request reaches a half-activated generation. |
| `applied` | Commit active/previous selection and success only after health passes; release admission, update effective engine evidence identity, then permit cleanup. A stopped Model Hub stays stopped after isolated preflight/selection. |
| `rolling_back -> rolled_back` | Stop candidate; restore previous verified selection and run intent; health-check it before resuming. Keep the failure visible and quarantine the candidate identity until explicit retry or a different build. |
| `blocked` | Neither candidate nor a safe fallback can run: report an actionable dependency error and stop Model Hub only; Avibe and unrelated backends continue. |

Reuse `_routing_lock`, `_transports_idle`, installer claims and the supervisor,
but split existing `ensure` into staging and activation: today it changes the
pointer before restart and does not roll back a failed restart. Do not layer a
second updater around that behavior. Durability order is explicit:

1. Persist the verified candidate and both generations' manifests/archives.
2. Durably write `intent(old, candidate, run_intent)` **before** stopping old CPA.
3. Write the candidate pointer; start/health-check only under the activation fence.
4. Durably commit `(active=candidate, previous=old)` **before** success/admission
   or cleanup. A pointer is a projection, not the recovery source of truth.

On restart, intent without commit restores old; durable commit repairs a stale
pointer to candidate; no pending intent uses the last committed generation. A
revoked/incompatible recovery target is never restarted; use the permitted safe
fallback or block Model Hub. Stop surviving owned CPA processes only after checking
recorded process identity. POSIX writes require file fsync, atomic replacement and
parent-directory fsync; Windows needs tested native flush/write-through semantics.
Plain atomic rename is insufficient. An unsupported durability primitive blocks
automatic activation rather than claiming power-loss safety. Inject failure at
every write/flush/stop/start boundary, including commit-with-stale-pointer recovery.

Do not automatically replay a request already sent upstream, especially one
that streamed or invoked a hosted tool. Existing transports drain unchanged;
not-yet-admitted requests wait within their caller budget or receive a retryable
local unavailable result. Ordinary updates may remain pending under sustained
activity. OAuth flows and credential mutations share admission leases, not only
inference calls. Do not run two real-auth CPA processes side by side.

Rollback restores binaries/selection, **not** a stale snapshot of live credentials.
Backward-state compatibility is an eligibility gate: run the exact candidate on
synthetic Avibe-rendered config and CPA credential documents, exercise supported
writes/refresh through loopback mocks with external egress denied, then read/use
those resulting files with each retained rollback/bundled baseline in the minimum
and current-stable qualification set. No production secrets or restored old-token
snapshots. Missing or failing round-trip evidence forbids promotion. An engine
requiring irreversible data migration needs an Avibe-coordinated change, not this
channel. Cleanup preserves bundled, active, previous, staged and journal-referenced
generations/archives.
Hosted-tool capability/refusal stamps must use the actual admitted engine identity,
not the current cached packaged-manifest stamp; swap and rollback invalidate new
resolutions without rewriting completed evidence.

## 4. Publication gate and emergency withdrawal

Separate a reviewed engine candidate record from the packaged default, so an
engine PR does not implicitly schedule adoption in the next Avibe release.
Extend the existing builder/guard and workflows; do not create a second builder.

1. Review exact source, patches, Go version, minimum and generated six-platform
   manifest. Build without publication secrets and verify the entire asset set.
2. Run full hermetic Model Hub E2E and scenario-evidence checks on these **exact
   candidate bytes**, using the offline manifest injection already supported.
   Qualify the declared minimum Avibe and current supported stable Avibe (one run
   if equal) using their immutable released wheel/installer artifacts in isolated
   runtime environments. The audited harness drives those installed runtimes;
   neither source-checkout imports nor version-label overrides qualify. Record
   Avibe artifact digests, resolved dependencies/toolchains and harness/fixture
   digests. The first public channel promotion therefore follows stable 3.2.6
   artifact availability. Subsequent Avibe release gates check the current
   eligible channel engine as well as their bundled default.
3. Run the hosted-wire A/B gate against the current eligible engine and candidate.
   Extend it to accept explicit manifests plus local head assets; comparing only
   packaged pins would miss independent releases. Intended differences require
   the existing label plus owner-reviewed rationale; that does not waive failed
   assertions or missing scenarios. Include each distinct compatibility baseline.
4. Require per-platform startup/version/health smoke checks for all six artifacts,
   focused lifecycle/rollback tests, and backward-state fixtures. Full protocol E2E
   remains Linux-based. Record exact runtime/test SHA, manifest digest, asset
   digests and CI run/attempt in a qualification receipt. Skipped, cancelled or
   failing required checks do not qualify; reviewed strict xfails follow existing
   policy, never a new blanket waiver.
5. A protected default-branch finalizer validates the receipt and owner publication
   authorization, publishes an immutable non-latest engine release, mirrors and
   reads back all assets, then signs and atomically promotes the new index. It
   never rebuilds different bytes after qualification. Availability precedes
   advertisement. Publication alone does not make an engine eligible.

Extend backup/recovery coverage to all advertised and rollback targets, not just
the packaged pin. Generic mirror/recovery jobs cannot promote releases or erase
revocations. Withdraw via an owner-authorized dispatch that adds a permanent
signed tombstone, names a previously qualified compatible rollback target, bumps
sequence, writes the index and purges/revalidates its 60-second cache. Do not
remove immutable assets or merely delete the entry. Normal updates remain
forward-only; an explicit authenticated revocation is the narrow downgrade
exception, even below the bundled default.

Clients refuse new activation of revoked engines, recheck eligibility immediately
before activation, and move an already active revoked engine to the named safe
fallback at the next check. For withdrawal, block new admissions while draining;
after at most five minutes cancel remaining work with an explicit withdrawal
error (no replay), then switch. If fallback is unavailable/incompatible/revoked,
stop Model Hub rather than restart the known-bad engine. Retain tombstones across
renewals and restarts. Polling plus drainage is not instant global recall: offline
machines cannot learn a new revocation until they reconnect.

## 5. Dependencies, API and CLI

Keep the existing **Model Hub engine (CPA)** Dependencies row, not a new settings
page. Show upstream **and Avibe build revision**, active/default/available version,
minimum Avibe when blocked, last check, automatic-update setting, pending reason,
and last applied/rolled-back result. “Update now” is distinct from reinstall;
rollback is a visible warning while the restored working dependency remains ready.
A status read never starts CPA or performs a download.

Extend `GET /api/dependencies?id=model-hub-engine` additively with
`active_release`, `default_release`, `available_release`, `min_avibe_version`,
`auto_update`, `update_state`, `last_checked_at`, `last_result`, and operation ID.
Keep `version/latest_version/has_update` truthful for older consumers; the target
must not masquerade as the running process. Add authenticated/CSRF-protected
`POST /api/dependencies/model-hub-engine/check` and `/update`; reuse install-job
polling and return the same durable operation for concurrent callers. Existing
`/install` repairs the effective target, not necessarily the packaged pin.

Proposed CLI (new surface, not commands that exist today):

```text
vibe runtime engine status --json
vibe runtime engine check --json
vibe runtime engine update --json
vibe runtime engine auto-update on|off
```

All call the same service/IPC operations and expose identical states; an accepted
background job is not success. Return an operation ID and an explicit pending or
completed result. Existing Doctor and `runtime prepare/clean` use the same target.

**Recommend an off switch**, persisted as
`update.model_hub_engine_auto_update: true` by default, independent of Avibe's
own `update.auto_update` and Model Hub enablement. Use the existing permissively
loaded update section: adding a field inside today's strict `model_hub` object
would break downgrade loading. Verify down/up policy persistence as well as source
preservation. Turning it off prevents routine automatic adoption, not checking,
explicit Update now, repair of the already selected/default target, or withdrawal
of a known-bad build. It must not silently adopt the newest engine under “repair”.
State that exception beside the switch. An explicit Retry after a failed update
clears only that candidate's quarantine. Explicit offline mode suppresses network
operations; known locally recorded revocations still apply. Local/test manifest
overrides opt out of the public channel and are visibly identified. Use English/
Chinese catalog keys; implementation includes dependency-row design frames and Show
review.

## 6. Delivery, acceptance and owner decisions

After approval, deliver four bounded PRs, in contract order (no stacked branches):

1. avault keyless verifier + shared signed-envelope vectors and custody docs.
2. Avibe channel contract/resolver, pinned verifier integration, gated producer,
   renewal/withdrawal workflow and release docs; no client adoption until complete.
3. Transactional engine lifecycle, shared auto/manual updater, recovery, effective
   evidence identity and all startup/repair/cleanup consumers.
4. Dependencies/CLI/policy controls, design frames, English/Chinese public docs,
   and end-to-end adoption/rollback qualification. Coordinate public-docs repo
   changes as needed. Owner approves each PR before merge.

Primary acceptance: wrong signature/hash/platform/minimum and replay cannot execute;
failed download/startup and power loss preserve or restore a verified compatible
engine; concurrent UI/CLI/startup/cleanup never double-swap; active streams and
OAuth state survive routine updates; same upstream/new build is distinguishable;
Avibe upgrades/downgrades and offline restart do not accidentally repin; rejected
candidates do not loop; revocation beats a stale promotion and cannot be undone by
mirror recovery; production data never enters hermetic tests. Use focused owning-
boundary tests, CI native-platform smokes, and the repository local Incus runner.
No install, deploy, load test or restart on the owner's desktop.

**Estimate:** 7–10 focused engineering days after design approval and verifier/
release-signer decisions, plus review/CI and owner-held publication time. The
uncertainty is cross-platform activation/recovery and signer commissioning, not
the Dependencies button. Do not weaken authentication or rollback to meet 3.2.6.

**Owner decisions (recommended answers):**

1. Approve the signed-index + avault verifier dependency? **Yes**; an unsigned
   mutable index is not equivalent to today's packaged integrity pin.
2. Approve default-on updates with an off switch, but mandatory signed withdrawal
   handling including bounded termination of stuck work? **Yes**, with explicit UI
   wording; otherwise define a different withdrawal policy before implementation.
3. Approve this four-PR scope/estimate for 3.2.6, with each merge owner-held and
   strictly after stable 3.2.5 publication? **Yes**; first public channel minimum
   is 3.2.6, not a retroactive update capability for 3.2.5.

Out of scope: upstream auto-promotion, beta channels, arbitrary download URLs,
manual version pinning, remote workspace proxying, OS package self-update,
rebuilding other managed dependencies around this channel, credential migrations,
full TUF/delegated online key rotation, and a guarantee that local health checks
can detect every provider-side behavioral regression.

## Step 2 as built

What the second PR adds, where it lives, and the details it fixes that the
sections above left open. Release-side procedure is in `docs/release.md`.

**Code.**

- `core/model_hub_engine_manifest.py`: the strict six-asset manifest schema,
  moved out of the release guard so clients and publication share one
  validator. It now also rejects a repeated field.
- `core/model_hub_engine_channel.py`: envelope and payload parsing, the fixed
  domain digest, shipped trust roots, the keyless avault verifier, anti-replay
  state, and selection.
- `vibe/model_hub_runtime/engine_channel.py`: `EngineChannel`, this machine's
  view of the channel over the engine installer. Nothing in Avibe calls it yet.
- `scripts/model_hub_engine_channel.py` and
  `scripts/model_hub_engine_qualification.py`, with the workflows
  `Publish Model Hub engine channel`, `Qualify Model Hub engine candidate` and
  `Rehearse the released-wheel Model Hub E2E`.
- `scripts/model_hub_engine_release_guard.py`: verification, backup and
  recovery now cover every release the index lists.

**Uncommissioned by default.** `engine_channel_roots.json` ships with no root.
A client with no root makes no channel request, runs no verifier and adopts
nothing; every publication step stops before the network, the bucket, or
avault. A missing or damaged roots document counts as no root.

**Wire details fixed by the client.**

- The envelope has exactly `schema_version`, `key_id`, `payload`, `signature`.
  `signature` is lowercase hex. `payload` and each `manifest_base64` are
  standard padded base64 with a single encoding of their bytes.
- The payload is parsed only after its signature verifies, so unauthenticated
  bytes never reach the manifest parser.
- Every object has exactly its listed fields; a repeated field is an error.
- Bounds: envelope 2 MiB, payload 1.5 MiB, manifest 16 KiB, 64 releases, 256
  withdrawals, a 512-character reason, sequence below 2^53.
- `expires_at` is at most seven days after `issued_at`. A payload dated more
  than ten minutes ahead of the local clock is not yet valid.
- `min_avibe_version` is a plain `X.Y.Z`. Every `rollback_to` must name a
  release the same payload carries.
- Sequences increase; they need not be consecutive.

**Verifier.** Two bounded processes per check: `avault version`, then
`avault verify`. The binary comes from Avibe's read-only path lookup. The
caller never installs or upgrades avault, selects no store, and removes
`AVAULT_*` from the child's environment. Only exit 0 with `{"valid": true}`
verifies. Until the managed avault pin reaches 0.1.10 (#3517), a managed
install reports the verifier as too old and the channel stays closed.

**Recorded state.** `<engine runtime dir>/channel/state.json`, separate from
the install claim. It holds the highest accepted sequence, that payload's
digest, the accepted envelope, every withdrawal, and the minimum and manifest
digest of every release ever listed. A release that stops being listed keeps
its recorded identity, so it cannot come back with other bytes. Acceptance
runs under a cross-process lock against the file's current contents. A
rejected index changes nothing. Damaged state fails closed; it is never
treated as a fresh start. On read the stored envelope is authenticated again.
One that has expired, or that is older than the sequence floor the running
Avibe ships, is returned marked not fresh: its withdrawals still apply, and
it authorizes no adoption.

**Selection.** `resolve` returns one of four actions. A withdrawal of the
engine in effect is decided first and by itself.

| Action | When |
| --- | --- |
| `withdraw` | The engine in effect was withdrawn, and the one fallback its tombstone names is listed, not withdrawn, not quarantined, has an asset for this host, and its minimum Avibe is provably met. This may go below the bundled engine. |
| `blocked` | The engine in effect was withdrawn and that fallback is not usable. Nothing else is substituted: not the fallback's own fallback, and not a newer release. |
| `adopt` | Otherwise, a listed release is newer than the engine in effect, not below the bundled one, not withdrawn or quarantined, has an asset for this host, and its minimum is met. The newest such release wins. |
| `keep` | Otherwise. Nothing newer is eligible, the index is stale, routine updates are off, or the build names no released Avibe. |

A withdrawal does not depend on the index being fresh, on routine updates
being on, on the bundled engine's identity, or on this being a released Avibe
build. A stale index or offline mode still applies it but authorizes no
download, so the fallback must already be on the machine. A build that names
no published release (a source checkout, a dev or local version) cannot prove
any fallback compatible, so a withdrawn engine there is `blocked`, never
kept; with an engine nobody withdrew it simply adopts nothing.

If the recorded index cannot be authenticated when it is read, nothing is
adopted from it. That covers avault being missing or too old, and an Avibe
build with no readable root: a downgrade to a release that ships none, or a
roots document that is missing or damaged. If the recorded state names the
engine in effect as withdrawn, the result is `blocked`: the engine is not
kept, and no fallback is taken from an index that could not be checked. The
keyless case reads the record and nothing else: no request, no verifier. A
machine with no record, which is every machine before commissioning, keeps
its engine and creates no state.

A manifest override is the one documented way out of the channel, withdrawals
included. It takes precedence whether or not this Avibe ships a root.

**Publication.** Desired state is a ledger on the signer host, not the
published index. It holds the next sequence, the exact payload last
allocated, and every release identity and withdrawal ever allocated. Each
change is planned from it. The payload is committed to the ledger before
avault signs it, and the envelope before it leaves the job, so one sequence
is only ever signed over one payload, and a failed run is finished by
publishing the identical envelope again. The published object, the bucket
and workflow artifacts are never read as state.

The ledger is the trust anchor on the publishing side and must not go
backward. If it is rolled back together with the published index, this scheme
cannot detect it. The comparisons with the published index that the signer
and the writer make are diagnostics for a ledger that is behind; they are not
that protection. Ledger loss or doubt stops the channel pending a separately
approved recovery.

**Qualification status.** The receipt requires six evidence classes. Four have
producing jobs. `build` reuses the release guard's strict rebuild, which
already publishes the packaged engine. The per-platform smoke producer runs in
pull-request CI on all six platforms against the packaged engine, which shows
the producer works and qualifies nothing. The hosted-wire job has not run: it
needs a candidate. The released-Avibe job has not run for qualification: it
needs published 3.2.6 artifacts. Its steps are rehearsed in pull-request CI
against a pinned 3.2.6 release candidate. `backward_state` and `lifecycle`
have no producer until steps 3 and 4, so no receipt can be produced before
then.

**Not in this step.** Adoption, activation, scheduling, recovery, the
effective-engine consumers (step 3); the Dependencies row, CLI, policy switch
and public documentation (step 4); and all commissioning, including whether
renewals run unattended.
