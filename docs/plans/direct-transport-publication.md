# Direct-transport sidecar distribution

## Contract and scope

This is the five-platform **avibe-direct-transport sidecar**, not the Apple
`IrohLib.xcframework` release. It preserves stock/unpatched iroh, the existing
admission/security contract, and the 6 s / 1.5 s timing choice. Source merge,
artifact publication, consumer pin promotion, installation and enablement are
separate actions. None implies the next.

The publication PR owns `scripts/direct_transport_release.py`, its fixtures,
`.github/workflows/direct-transport-release.yml`, and the qualification packaging
changes. The consumer lane owns `vibe/direct_transport_runtime_manifest.json`
and installation/lifecycle. Integration must wait for that scaffold to merge;
there is no stacked consumer change here.

## Artifact contract

The crate version is `0.1.0`; the distribution revision is only in
`direct-transport-v0.1.0-N` (`N` positive, no leading zero). Five existing
`avibe-direct-transport-<Rust target>.tar.gz` names remain unchanged:

| Consumer key | Rust target | Root binary |
| --- | --- | --- |
| darwin-arm64 | aarch64-apple-darwin | avibe-direct-transport |
| darwin-x64 | x86_64-apple-darwin | avibe-direct-transport |
| linux-arm64 | aarch64-unknown-linux-musl | avibe-direct-transport |
| linux-x64 | x86_64-unknown-linux-musl | avibe-direct-transport |
| win32-x64 | x86_64-pc-windows-msvc | avibe-direct-transport.exe |

Windows arm64 aliases the x64 artifact in the consumer; it is not a sixth build.
Each archive retains its root binary, manifest, Cargo.lock and feature evidence.
It additionally includes `LICENSES/`: target-resolved crate texts, package and
source provenance, required supplemental notices, and Rust distribution notices.
The shared `scripts/cargo_notices.py` collector uses the checksum-pinned policy
in `packaging/iroh-apple/license-sources.json` plus the sidecar-only
`license-policy-overlay.json`. `scripts/direct_transport_notices.py` composes
these deterministically without changing the Apple policy or allowing conflicting
overrides; the archive carries the composed policy, both input digests, and its
provenance digest. Unreviewed expressions or missing texts fail packaging. The
actual-lock fixture inventories 278 checksum-verified registry crates from the
five qualified target graphs; CI still runs collection on fresh real metadata.
Vendoring is only for source/notice verification, with no patch or dependency
change. This collection does not claim a legal review. Cargo.lock is checked out
with LF on every platform so Windows autocrlf cannot change source-proof bytes.

macOS signing/scanning and all native/Linux gates precede hashing. New packaging
requires a fresh qualification; the old #3542 workflow-only archives do not meet
the notice/provenance contract and cannot be promoted by this publisher.

The public set is exactly five archives, five `.sha256` files and
`direct-transport-release.json`. The record retains target manifests, immutable
Actions artifact IDs/digests, exact producer attempt/job IDs and both Linux
idle/network reports. The selected successful master backstop's run/attempt is
kept in separate `backstop-proof.json` gate evidence (and public-readback receipt),
not immutable release bytes: a later successful backstop rerun at the same SHA
must not change the already-uploaded record. The record contains a *pending
candidate*, not a published consumer pin. Advisory/experimental binaries are
never selected or uploaded. The NAT preparation/experiment remains separate.

## Named gates

1. **Source/PR gate.** Product reviews the exact-head report and gives go;
   the lead merges. Review and required PR CI must pass. No local native tools
   are needed for this implementation. Incus remains blocked until explicit
   3.2.6 stable acceptance and the lead's serialized slot; not running it must
   remain visible in the Merge-ready report.
2. **Master qualification gate.** Select an immutable commit on master with a
   successful full `master-full-backstop`. `avibe-direct-transport` must finish
   all five builds/tests at that same SHA on master (push or dispatch, not PR).
   Changes to sidecar packaging trigger master qualification automatically.
   Select one complete run attempt: inherited jobs from a partial rerun cannot
   qualify. GitHub artifact ZIP digests and upload timestamps bind each archive
   and Linux evidence set to its exact successful job. Missing proof fails.
3. **Preparation gate.** After separate Product dispatch go, run
   `direct-transport-release` on master with `publish=false` (default), exact
   `source_sha`, `qualification_run_id`, `qualification_attempt`, `release_tag`.
   The source must equal this workflow's immutable `github.sha`. Preparation has
   only contents/actions read permissions and emits a candidate packet. A green
   preparation is not publication or pin approval. If master has moved, select
   and fully qualify its new exact revision; do not quietly mix workflow policy,
   source, or old artifacts.
4. **Publication gate.** Product must give a separate exact source/tag/run go.
   The workflow's publication job uses environment `direct-transport-release`;
   under the current Product default-off trial policy this environment is plain
   with no required reviewers. The workflow/helper enforce master-only exact
   source matching and exact confirmation; no separate owner commissioning step
   is required. Revisit environment protection before ordinary-user enablement.
   Dispatch with `publish=true` and exact confirmation
   `publish <tag> <source_sha> <run_id>/<attempt>`. The write-capable job rebuilds
   its proof from authenticated, immutable Actions artifacts; it does not trust
   the preparation artifact as authority. Only this workflow creates the tag and
   draft release. Existing mismatched tags/assets fail; there is no clobber or
   tag movement. Both draft creation and finalization send `make_latest=false`.
5. **Public readback gate.** Read the exact tag target, draft/prerelease flags,
   public asset set, API sizes and SHA256 digests. Download **all eleven public
   assets without authentication or an authenticated fallback**, compare actual
   byte sizes/SHA256, and inspect each downloaded archive's binary/provenance.
   Read repository Latest before and after and require it to remain the same
   readable official stable release. API failure, a concurrent Latest change or
   unavailable public bytes cannot emit a published pin. `immutable` is reported
   as returned by GitHub, not inferred from a source SHA.
6. **Pin integration gate.** Only after gate 5 does the workflow emit
   `public-readback.json` and `direct_transport_runtime_manifest.proposal.json`
   as workflow evidence. Neither writes repository files. After the consumer
   scaffold has merged and Product gives pin-promotion go, use a fresh pin PR;
   verify the public evidence again and pass its exact-head review/CI. No
   fixture, manually entered digest or old candidate can be called published.

The schema1 proposal has `direct_transport_version`, `release_state=published`,
`source=avibe-bot/avibe`, exact `source_url` (`releases/tag/<tag>`), `release_tag`,
and all five `archives`. Each entry has `name`, exact release-download `url`,
positive byte `size`, `sha256`, `binary_sha256`, `format=tar.gz`, and root
`bin_path`. Extra provenance identifies source, qualification run/attempt and
the public release record's SHA256. The consumer hashes the complete manifest.

## Latest, updater and mirror isolation

The dedicated tag family does not match official `vX.Y.Z` or `gh-vX.Y.ZrcN`
selectors. Both current and legacy `update-notification=none` markers are in the
release body. This workflow does not dispatch official release, desktop,
notification, website or mirror jobs. The scheduled mirror may copy this
auxiliary full release into its immutable release namespace/index; its canonical
stable-tag/desktop-asset selector cannot turn it into a `latest/` installer.
Fixtures exercise the shared official selector, mirror latest selector and CLI
notification marker reader. Repository Latest also has a live post-publish gate;
configuration intent alone is insufficient.

## Failure and retry

A pre-publication failure leaves no published pin. An interrupted publication
may leave a matching tag/draft or an already-published release. Inspect the
failure and obtain authorization for the same exact tuple before retrying; do
not delete or replace bytes. The workflow can fill only missing matching draft
assets. An existing public release is read back, never edited; a missing public
tag fails proof without recreating the ref. A new successful backstop run/attempt
is recorded only in fresh gate evidence; it does not regenerate public bytes.
Public-readback
failure can occur *after* publication; report that state plainly and withhold
pin promotion. A later retry must re-prove all source/qualification/public gates.
Concurrent official releases intentionally invalidate the captured Latest
comparison; retry after inspecting the legitimate new baseline, not by changing
Latest back.

This workflow does not install, start a sidecar, request certificates, enable
transport, update a live runtime, or perform remote operations. Public artifacts
and green fixtures do not establish real-device roaming/privacy/firewall safety
or a universal failure deadline; the original stock exposure limitations remain.
