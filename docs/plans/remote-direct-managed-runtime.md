# Managed instance direct transport (I1/I3 regression preparation)

Status: consumer implemented, **code/config preparation only**, pending review
and CI. This work consumes the merged
[sidecar contract](remote-direct-transport.md) and
[I3 lifecycle](remote-direct-lifecycle.md). It neither publishes a native
release nor promotes a pin, installs a runtime, changes a live setting, or runs
Incus. The first operational exercise requires the lead's confirmation that
3.2.6 stable acceptance is complete and a serialized local Incus slot.

## Change contract

- Add a consumer of a **packaged, published, exact release pin**, reusing
  `core.managed_runtime` for download bounds, archive/binary SHA-256 checks,
  extraction, mutation serialization and atomic selection. No PATH lookup,
  custom manifest, URL, environment or arbitrary executable override.
- Installation is explicit and independent of enablement. It must not alter
  pairing, certificates, `remote_access.vibe_cloud.direct` or Cloudflare, and
  must not run the sidecar as a version/readiness probe.
- Pending, missing, malformed or unsupported pins cannot install or resolve a
  transport, even if an older selected installation exists. A changed pin
  requires a matching verified installation; there is no previous-version
  fallback for a network-facing optional transport.
- The lifecycle observes verified installed binaries off its owning loop.
  It must remain responsive to opt-out, pairing changes and permission expiry
  while filesystem work is stalled. Late observations cannot restore an old
  generation. Default-off performs no binary inspection or installation.
  Config has its own retained reader at 0.5-second intervals, independent of
  the single retained binary worker. Binary verification runs on changed config
  and otherwise every five seconds, with a 15-second observation expiry that
  withdraws the path even if its worker is stuck. Thus binary I/O cannot delay
  disk-only CLI opt-out and its false report. Retiring a generation clears only
  its cached certificate observation and requires a fresh client poll before
  restarting; a fenced renewal may already have retired the old chain files.
  This is bounded observation, not atomic exclusion of arbitrary filesystem changes.
- Only current setting + managed pairing/hostname + verified binary + fresh
  backend permission + usable certificate can reach sidecar start. A ready
  rendezvous additionally requires the current supervisor's ready event and
  eligible addresses. Normal DirectIngress and remote authentication remain.
- No certificate is imported or invented by the installer. The existing I2
  client owns `$AVIBE_HOME/state/direct/`, CSR/key creation, chain validation
  and renewal; installation does not touch that directory.

## Consumer manifest

`vibe/direct_transport_runtime_manifest.json` ships pending, with no archive
URLs or checksums. Its future promotion is a separate publication-lane PR.

The published shape uses the shared schema version 1 with:

- `direct_transport_version`, explicit `release_state: published`,
  `source: avibe-bot/avibe`, `release_tag: direct-transport-v<VERSION>-<N>` and
  `source_url` naming that exact GitHub release;
- `archives` for `darwin-arm64`, `darwin-x64`, `linux-arm64`, `linux-x64` and
  `win32-x64`; Windows ARM64 uses the Windows x64 archive, as specified in I1;
- each entry: the existing I1a archive name, exact release-download URL,
  positive byte size, archive and binary SHA-256, `format: tar.gz`, and
  `bin_path: avibe-direct-transport` (`.exe` for Windows).

The manager installs beneath `$AVIBE_HOME/runtime/direct-transport/`.
Publication, native signing/qualification, mirror inclusion and the release
availability guard belong to the I1 publication owner. Synthetic fixture bytes
used by unit tests are never a product pin or a regression-runtime substitute.

## Planned operator handoff (not executed)

Run only inside the runner-owned regression instance after its hold/slot and
stable acceptance gates are cleared. Preserve its existing pairing and state.
The I6 lane owns `scripts/incus_regression.py` usage and the actual scenario.

1. Read the installed Avibe revision and packaged manifest. Require the
   approved published release/pin, all platform assets and public readback
   evidence; do not substitute a workflow artifact or a local Rust build.
2. Use `vibe remote direct status --json` to record the initial choice. Inspect
   the regression pairing without exporting its credentials. It must use a
   managed `https://<slug>-app.avibe.bot` hostname and the intended test backend.
3. Run `vibe remote direct install --json` (or `--offline` only when the exact
   verified archive is already cached). Preserve the install result's pinned
   version/platform/hashes. Installation must leave the prior choice unchanged.
   An already opted-in owner can start using it after the other gates pass; do
   not mistake installation for an inert operation on an enabled instance.
4. The backend lane supplies an allowlisted paired regression fixture, normal
   user authentication and the certificate-order/current routes. The minimal
   overlay is `DIRECT_CERT_ENABLED=true` plus
   `DIRECT_CERT_ALLOWED_INSTANCE_IDS=<verified regression instance ID>` on the
   **same backend** receiving this pairing's heartbeats. If the instance is
   production-paired, stop for a scope decision rather than re-pairing it or
   changing its backend URL. Permission alone is insufficient: the issuer,
   account and DNS zone configuration and matching binding must also exist;
   the current-certificate route returns 503 when issuer configuration is
   absent. Real heartbeats require HTTPS. Certificate issuance must use that
   instance's locally generated CSR/key; no production keys, copied
   certificates, hosts-file bypass or TLS-verification disablement.
5. After explicit regression enablement authorization, use
   `vibe remote direct on --json`. Record prompt true reporting, effective
   `direct_access_allowed`, certificate readiness, then `running/published`.
   A status boolean alone is not proof that a client can connect.
6. I6 uses the normally authenticated same-origin rendezvous and admitted
   endpoint to verify CONNECT/TLS/auth, expiry/revocation, stale generations and
   fallback. Never publish the endpoint addresses, tokens or key paths in a
   user-facing report. Keep synthetic-CA hermetic evidence separate from public
   certificate/installed paired-instance acceptance.
7. Restore the initial direct choice with the normal CLI as authorized; do not
   unpair, reset config or remove user state. Record explicit false reporting
   and rendezvous withdrawal when returning to OFF. Confirm Cloudflare remains
   usable throughout.

## Acceptance and dependencies

Focused tests cover pending/invalid/stale pins, each platform mapping,
verified installation, tamper refusal/repair, installer authority/no enablement,
loop responsiveness and stale binary observations, and the publication gate.
An installed inert archive is exercised through the production resolver and
lifecycle with fake certificate/transport owners; no native binary runs in this
proof. Existing certificate and ingress tests remain the owner of key/chain
validation and ordinary TLS/auth behavior; no duplicate certificate provisioning
path is added. PERMISSIONS-026 covers the new Owner-only install command.

Local focused validation: 1,019 tests passed across the managed-runtime core,
new consumer, lifecycle, certificate, transport, ingress, rendezvous, remote
access, CLI and invocation-authority suites. The CLI status handoff also exposed
and fixes a missing runtime `V2Config` import in the existing direct-status
command; its regression reads a real isolated persisted choice. These are
hermetic Python tests, not installed native/Incus/public-issuance evidence.
The independent review's two lifecycle regressions have dedicated reproductions:
real CLI disk-only opt-out while the binary hash remains blocked, and discarded
renewal retiring the old chain before binary recovery. Both fail on the first
consumer head (delayed withdrawal and a latched startup failure respectively)
and pass with independent config observation and fresh-generation certificate
revalidation.

Operational acceptance is **not run**. Prerequisites: merged consumer, a
published/pinned qualified sidecar, backend allowlist/certificate fixture,
3.2.6 stable acceptance completion, and the lead's local Incus slot. The first
installed-readiness smoke is estimated at 30–45 minutes after those inputs are
ready; the complete I6 scenario has its own estimate and does not depend on
App A2 being linked to prove the instance boundary.
