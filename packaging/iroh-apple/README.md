# iroh iOS binary qualification gate

This directory owns the Phase 0 qualification gate for the iOS app's embedded
`iroh-ffi` dependency. It is deliberately separate from `ios/`: the app team
will consume the workflow artifact in Phase 1.

## Pin and feature policy

The gate checks out `n0-computer/iroh-ffi` tag `v1.2.0` at commit
`66a8d619a4d791d458d582f312df6e0ab42bc541`. Its lockfile resolves iroh core
and iroh-relay `1.4.1`, and noq/noq-udp `1.4.0`.

The checked-out `Cargo.toml` is patched before any build to replace iroh's
implicit defaults with:

```toml
iroh = { version = "1.0.0", default-features = false, features = ["metrics", "tls-ring"] }
```

`metrics` is used by the FFI endpoint surface and `tls-ring` is required for
the TLS backend. `fast-apple-datapath` is not enabled, and `portmapper` is
also intentionally dropped because unsolicited UPnP/NAT-PMP/PCP mapping is
not wanted. Multicast and mDNS/local-network address discovery are also not
allowed: the gate asserts that no matching feature or crate is present in any
resolved iOS graph. Phase 1 supplies LAN addresses from the rendezvous instead
of discovering peers locally, so this does not depend on an Apple multicast
entitlement.
The gate excludes iroh's `portmapper` feature; the feature graph may still show
that crate because `iroh-services` v1.0.0 uses it for its own diagnostics
dependency, which is distinct from enabling iroh's unsolicited mapping feature.
The published `irpc-iroh` manifest (pulled by `iroh-services`) otherwise requests
iroh's default features, so the build script vendors the upstream lockfile
dependencies and patches only that manifest's iroh edge to `default-features = false`;
the upstream `Cargo.lock` remains the lockfile and every Cargo command uses
`--locked`. Every iOS target's resolved feature graph is checked for
`fast-apple-datapath` on iroh/noq/noq-udp, for iroh's `portmapper` feature, and
for multicast/mDNS/local-network address-lookup features or crates. The
manifest records both the declared iroh features and the complete resolved iroh
feature set; in this pin, `unstable-net-report` is pulled transitively by
`iroh-services`.

## What the gate proves

The resolved graph also compiles the `iroh-services` binding, including n0's
hosted telemetry client, and the transitive `portmapper` crate. Neither runs
unless application code constructs `ServicesClient`; Phase 1 must not
construct or use it. A later patch may remove that binding and its transitive
code, but this Phase 0 gate does not change the upstream API surface.

On GitHub Actions `macos-26` with the repository's pinned Xcode 26.6 path, it:

1. verifies the exact upstream commit and applies the small feature patch;
2. builds the Rust static library for `arm64` iOS and a universal `arm64 + x86_64`
   iOS Simulator slice with `IPHONEOS_DEPLOYMENT_TARGET=17.0` and `--locked`;
3. generates the UniFFI Swift/C bindings and assembles an iOS-only
   `Iroh.xcframework` using `xcodebuild -create-xcframework -library`;
4. builds the upstream `IrohLib` Swift package for generic iOS device and
   generic iOS Simulator destinations with a 17.0 deployment target and signing
   disabled;
5. installs the lowest available iOS 17.x Simulator runtime below 17.5 and runs
   the upstream `IrohLibTests` XCTest suite on that runtime;
6. scans every xcframework slice and every Mach-O/archive product produced by
   those Swift builds for both `sendmsg_x` and `recvmsg_x` as symbols and as
   literal strings; and
7. verifies the same checker **does** find the private names in the upstream
   prebuilt `IrohLib.xcframework.zip` after checking its published SHA-256, and
   requires a hit in that archive's `ios-arm64` slice specifically.

The runtime test proves that the built Swift package can load and execute the
upstream XCTest suite on one installed iOS 17.x Simulator runtime below 17.5.
It does not prove behavior on a physical device, every iOS 17.0 hardware
configuration, or code paths that the upstream tests do not exercise. The gate
also fails if xcodebuild emits a warning that an object was built for a newer
iOS deployment target than the one being linked.

A cheap static imported-symbol inventory against the current SDK can show which
system symbols the device archive references, but it cannot establish their
availability on iOS 17.0 without an iOS 17.0 SDK. The runtime test is therefore
the authoritative compatibility check here; no separate availability checker is
built in this phase.

The artifact contains the iOS-only xcframework zip, its SHA-256, the generated
`IrohLib.swift` binding, feature-graph captures, build logs, runtime-test logs,
and `manifest.json` with pins, declared and resolved iroh features, toolchain
versions, deployment target, tested runtime, and slice layout.

## What the gate does not prove

It does not archive, sign, install, or submit the App; it does not validate
TestFlight/App Store packaging, entitlements, runtime networking, peer
authentication, relay behavior, or the Phase 1 loopback CONNECT integration.
The resulting xcframework is the **only iroh binary an iOS build may link**;
this exclusion applies to every iOS build, including development, ad hoc,
App Store, and TestFlight archives. The Phase 1 app archive must repeat the
binary check on its final linked products (including any transitive/static-link
result). Do not add that check to `ios/scripts/testflight.py` in this phase.

## Optional publication and consumer pin

`release.json` is the single repository pin. It starts in `pending` state with
no zip checksum: **consumers must reject pending pins**. The separate build
manifest describes a CI qualification; it is not proof that a release exists.
The qualified 17.0 artifact serves iOS 17.0 and later; there is no parallel 17.5
publication to select.

A manual dispatch may supply `release_tag=iroh-apple-v1.2.0-1`, matching
`IROH_FFI_TAG` and `IROH_APPLE_REVISION` in `versions.env`. `dry_run` defaults to
true. The read-only manifest job runs after the full gate, verifies the
same-run files, and records URLs, byte counts and SHA-256s. Regular PR runs keep
the existing checks and the original qualified artifact; the additional
support bundle and pending release-candidate manifest are separate artifacts.
If the gate falls back to another runtime below 17.5, its qualification remains
valid, but publication refuses unless the tested runtime equals the deployment
target exactly. A no-tag PR dry run records that runtime without publishing it.

Only an explicit non-dry-run dispatch in `avibe-bot/avibe` on `master` can enter
the publication job. Only that job has `contents: write`; PRs and other branches
cannot publish. It revalidates the same-run candidate and refuses mismatched
pins, patch/build recipe hashes, missing evidence/licenses, checksum changes,
failed tests, and existing tags or releases (including drafts). Collision/API
lookup failures are not absence. Publication is serialized, targets the tested
source SHA and never overwrites assets. A failure after creating a release is
not automatically repaired: diagnose it and use a new reviewed revision.

Both an explicitly tagged dry run and publication preflight require the
repository's full master `lint.yml` backstop to have passed at the **exact
source SHA**, including its successful `master-full-backstop` terminal job.
They reuse `scripts/ci_release_backstop.py --source-sha` before creating any tag.
Missing, running, failed, skipped/coalesced or unreadable proof blocks the
release; a green run for another SHA or a PR is insufficient. Wait for or rerun
the full backstop at that SHA, then dispatch again. The no-tag PR dry run does
not require master certification of a PR commit. Proof queries have only
`actions: read`, and the helper's verified run ID is recorded in the job log.

**Owner approval is procedural, not a technical reviewer lock.** No protected
environment is created here; a repository writer can dispatch with dry run off.
Merging this mechanism does not approve its first use or publish anything.

Release assets and layout:

- `IrohLib.xcframework.zip` is byte-for-byte the checked zip from the same run.
  Its top-level directory is `Iroh.xcframework/`, containing `Info.plist`,
  `ios-arm64/libiroh_ffi.a` and
  `ios-arm64_x86_64-simulator/libiroh_ffi-ios-simulator.a`; each slice has
  `Headers/Export.h`, `Headers/iroh_ffiFFI.h` and `Headers/module.modulemap`.
  Use SwiftPM `.binaryTarget(name: "Iroh", url: ..., checksum: ...)`, or download
  and SHA-256-verify before extracting. This is a C/UniFFI binary, not the Swift
  wrapper by itself.
- `IrohLib-support.zip` contains the matching `IrohLib.swift`, upstream
  `LICENSE-APACHE` and `LICENSE-MIT` under `LICENSES/iroh-ffi/`, and license/NOTICE
  texts from locked dependencies indexed by `LICENSES/packages.json`, plus the
  Rust distribution's copyright notices and installed `licenses/` directory.
  This deliberately includes build/host dependencies too. Consumers must obtain
  and preserve this bundle's notices with the redistributed binary and wrapper.
  The binding is also published as `IrohLib.swift`; both forms are checksummed.
- `iroh-apple-release.json` records tag, binary URL/checksum, upstream commit,
  **resolved iroh core version**, patch/build recipe hashes, target/runtime,
  source commit/run and each asset's URL, size and SHA-256. Qualification logs,
  feature graphs, runtime-test JSON and the build manifest accompany it.

After an owner-approved publication, independently read the release back and
**download every named asset**. The promotion PR copies the verified release
manifest into this directory's `release.json` with `release_state: published`,
and includes read-back asset names, byte counts and computed SHA-256s as review
evidence. Never promote based only on a dispatch result or an anticipated URL.
The publication workflow cannot push the pin to master.

This tag family is not a product version. Publication explicitly sets
`make_latest: false` and the silent update-notification marker. Product release
selection and desktop updates accept only their canonical `v…`/`gh-v…` families.
The existing hourly release mirror naturally retains this full release and its
assets; its `latest/` installer selection requires product tags and desktop
installers, so this release cannot replace those aliases. GitHub URLs remain
the pin; no new mirror guard or backup infrastructure is added.

## Bumping the pin

1. Update `versions.env` with the new tag, exact commit, resolved versions,
   upstream release checksum, and Rust/toolchain values.
2. Inspect the new upstream `Cargo.toml`, lockfile, `make_swift.sh`, and
   `Package.swift`. Update the feature patch only if the dependency line
   changed; keep `default-features = false` and re-evaluate the smallest
   compiling feature set.
3. Run the workflow manually and confirm that the positive control still
   contains both private API names. If it does not, stop and investigate
   rather than weakening the checker.
4. Run the workflow manually and confirm that the positive control still
   contains both private API names and that the iOS 17.x runtime test passes.
   Record the runtime selected by CI in the artifact manifest and PR body. If
   no runtime below 17.5 can be installed with the pinned Xcode, stop and
   report the lowest runtime that was available instead of weakening the gate.

The workflow intentionally runs only on `workflow_dispatch` and pull requests
that touch this gate or its workflow; it is not part of the normal PR lint
fan-out.
