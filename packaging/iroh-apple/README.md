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
manifest (pulled by `iroh-services`) otherwise requests iroh's default
features, so the build script vendors the upstream lockfile dependencies and
patches only that manifest's iroh edge to `default-features = false`; the
upstream `Cargo.lock` remains the lockfile and every Cargo command uses
`--locked`. Every iOS target's resolved feature graph is checked for
`fast-apple-datapath` on iroh/noq/noq-udp, for iroh's `portmapper` feature, and
for multicast/mDNS/local-network address-lookup features or crates.

## What the gate proves

On GitHub Actions `macos-26` with the repository's pinned Xcode 26.6 path, it:

1. verifies the exact upstream commit and applies the small feature patch;
2. builds the Rust static library for `arm64` iOS and a universal `arm64 + x86_64`
   iOS Simulator slice with `IPHONEOS_DEPLOYMENT_TARGET=17.5` and `--locked`;
3. generates the UniFFI Swift/C bindings and assembles an iOS-only
   `Iroh.xcframework` using `xcodebuild -create-xcframework -library`;
4. builds the upstream `IrohLib` Swift package for generic iOS device and
   generic iOS Simulator destinations with signing disabled;
5. scans every xcframework slice and every Mach-O/archive product produced by
   those Swift builds for both `sendmsg_x` and `recvmsg_x` as symbols and as
   literal strings; and
6. verifies the same checker **does** find the private names in the upstream
   prebuilt `IrohLib.xcframework.zip` after checking its published SHA-256.

The positive control is important: a checker that never fires is not evidence
that the patched artifact is clean.

The artifact contains the iOS-only xcframework zip, its SHA-256, the generated
`IrohLib.swift` binding, feature-graph captures, build logs, and `manifest.json`
with pins, toolchain versions, deployment target, and slice layout.

## What the gate does not prove

It does not archive, sign, install, or submit the App; it does not validate
TestFlight/App Store packaging, entitlements, runtime networking, peer
authentication, relay behavior, or the Phase 1 loopback CONNECT integration.
The resulting xcframework is the **only iroh binary an iOS build may link**;
this exclusion applies to every iOS build, including development, ad hoc,
App Store, and TestFlight archives. The Phase 1 app archive must repeat the
binary check on its final linked products (including any transitive/static-link
result). Do not add that check to `ios/scripts/testflight.py` in this phase.

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
4. Record the new artifact's manifest and checker output in the PR body before
   handing the gate to Phase 1.

The workflow intentionally runs only on `workflow_dispatch` and pull requests
that touch this gate or its workflow; it is not part of the normal PR lint
fan-out.
