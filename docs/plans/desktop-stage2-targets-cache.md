# Desktop Stage 2: channel targets and trusted warm caches

## Change contract

Owner decision (2026-11-11): RC desktop releases build Apple-silicon macOS and
Windows only; stable still requires Apple silicon, Intel macOS, and Windows.
The complete release set is selected by canonical tag, never by which jobs
happened to succeed. Every built platform retains its OS signing, notarization,
updater signatures, source provenance, digest checks, and immutable attachment.

- `desktop_release.release_targets(tag)` owns the channel's publication set.
  Missing required targets and extra target artifacts fail verification.
- Intel updaters keep the existing target-specific discovery behavior: an RC
  lacking their manifest, manifest signature, and payload is not a candidate.
  Historical RCs that actually shipped a complete Intel set remain discoverable.
  Stable releases supersede RCs normally; no downgrade is offered.
- The mirror indexes only uploaded asset names. RCs never populate `latest/`
  installer aliases; those require a complete three-platform stable set.
- Cua Driver reuse remains verified. Trusted master warming covers both Macs;
  releases only restore caches, warn visibly on misses, and rebuild when needed.
  Exact-source compile keys also bind the lockfile (Cargo's third-party source
  checksums), target, compiler, and SDK. Cached compile outputs never stand in
  for final version stamping, packaging, signing, notarization, or verification.
  The master warm-up is the only cache writer; release and PR builds are
  restore-only. After a successful warm-up, maintenance retains one newest
  entry per release target and deletes superseded entries plus retired PR shell
  caches. The retained release set is budgeted at 1.6 GB (about 0.2 GB above
  the observed 1.4 GB set), leaving the rest of the repository's 10 GB cache
  budget for other workflows; maintenance reports total usage and headroom.
  A failed push/schedule warm-up gets one workflow-dispatch retry, and release
  cache misses remain visible warnings rather than a correctness bypass.
- Do not add an Intel runner to every RC indirectly through mandatory prewarming.
  Document the early Intel build signal and the remaining stable-only signing
  coverage. Cache warming is an optimization, not a new release correctness gate.
  The Intel warm-up is restricted to `desktop/**` (excluding Markdown), `uv.lock`,
  and release helper/workflow inputs, runs with concurrency one and cancels a
  superseded master warm-up. The lookup-only keep-alive runs on master pushes
  and every five days; a missing, unreadable, or unrefreshed entry warns the
  operator to run the next warm-up. No additional five-day warm-up was added;
  the existing warm-up remains the writer and the keep-alive is an observation,
  not a release correctness gate.

## Scope and delivery

No x64-on-arm build, Stage 1 UI reuse, new release/tag, signing credential changes,
or local Docker/native release builds. Focused hermetic tests run locally;
normal PR CI and exact-head Codex review are required. Land after the RC gate
(#3545) and the independent Stage 1 speed-up. The product owner approves the
exact head; the designated release lead merges. Do not dispatch a release to
measure this change.

## Validation

- Real asset producers/verifiers: RC two-platform set passes; either required
  platform missing fails; extra Intel RC files fail; stable requires all three.
- Signed updater verifier visits exactly the channel's targets and rejects
  missing/tampered payloads or signatures on any required target.
- Existing Rust discovery tested with both raw GitHub-shaped releases and a
  mirror index: Intel skips new RC, Apple silicon/Windows find it, and stable
  remains available with prereleases enabled or disabled.
- Mirror plans/indexes preserve the stable aliases and never invent Intel RC
  download paths, including core-only and partially attached Stage 2 releases.
- Workflow tests cover actual matrix selection, trusted cache writers, exact
  key inputs, miss fallback, and unchanged signing/verification steps.

## Measurement baseline and follow-up

RC5 desktop run `42953973959`: dispatch 2026-11-11 03:37:24 UTC, attachment
completed 04:34:24 (57:00). Mirror run `42960110295` completed 04:39:17
(61:53 from desktop dispatch). Intel queue after resolver completion was 15:35;
Intel execution 33:09 vs Apple silicon 16:32. Both Intel Rust and Cua caches
missed. The original preliminary profile incorrectly counted preflight as
runner queue and mixed RC4 attempts; do not reuse those queue estimates.

After landing, observe (do not trigger) the next two RCs. Record exact source,
attempts, selected targets, cache hits, queue separately from work, package
execution, signing/finalization, attachment and mirror completion alongside
RC5. Only these real runs prove hosted cache availability, Apple/Certum service
latency, and end-to-end time. The next stable proves the retained Intel signed
release path; unsigned master warming is not a replacement for it.
