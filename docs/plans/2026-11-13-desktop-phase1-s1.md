# Desktop Phase 1: native account and client selection

## Change contract

Keep RuntimeHost and LoopbackOrigin local. Add one native account and client
selection coordinator, reached only through the enumerated main-window bundled
account page. Runtime documents receive a nonsecret projection and presentation
actions. Account cookies and enrollment/OIDC material remain native; TOTP
seed/QR/recovery display uses direct trusted replies only.

A target is resolved by ID from a fresh authorized catalog. Require canonical
HTTPS and current matching remote session identity before projecting host-scoped
cookies to a fresh disposable WebView store. All asynchronous completions are
fenced by account/selection generation. Local lifecycle continues while remote
is selected. Sign-out erases native credentials and remote stores even offline,
returns local, and preserves pairing and remote-access intent.

## Validation boundaries

The first native probe establishes cookie attributes and isolation using only
test-owned stores. Then coordinator tests exercise actual HTTP producers and
shared fixtures. A reusable integration runner joins exact candidate backend,
Runtime and bundled UI artifacts without importing peer commits into this PR.

AUTH-SETUP-171/175/176/177/179 cover full authentication, selected identity,
late completion/local continuity, privileged caller isolation and explicit
remote auxiliary disable. Existing shell tests only cover a local Runtime;
these tests protect the newly introduced account and selection boundaries.

All tests use private HOME/XDG/config/credential/DB/endpoints. Signed-package
Keychain retention, passkey association/system sheet, WebView platform behavior
and hit testing remain RC acceptance. No signing, installation, merge or
service restart belongs to this change.

## Shared handoff

`desktop/contracts/instance-client-v1/` provides JSON fixtures for the approved
inner envelope, direct TOTP responses, native projection and HTTP boundaries.
The bridge's outer command argument is `request`. Compatible bundles expose
`account/index.html`; view is enumerated presentation only.

The existing updater remains owner. A trusted Phase 1 `avibe://updates`
presentation must reach its native confirmation directly, without redispatching
to the same Workbench footer. Cancellation and stale generations cannot install.

## Native cookie probe

Build and run from the checkout root:

```sh
cargo build --manifest-path desktop/Cargo.toml -p avibe-desktop --example instance_cookie_probe
python3 desktop/src-tauri/tests/run_cookie_probe.py \
  desktop/target/debug/examples/instance_cookie_probe --log /tmp/instance-cookie-probe.log
```

The runner creates empty HOME/XDG directories and passes only an explicit
environment allowlist. The executable builds two hidden, disposable real Tauri
WebViews, starts only its own loopback HTTP fixture, and never starts Runtime or
calls Keychain. It projects the actual `__Host-vibe_remote_session` cookie name
for an HTTPS host without making a network request to that host. Checks cover
Secure/HttpOnly/SameSite, exact host (including rejecting a child domain),
separate WebView stores, JavaScript HttpOnly exclusion through actual HTTP, and
erasure. An independent run of the first probe exposed that Tauri's
`clear_all_browsing_data` returns after scheduling work, not after deletion.
That initial green run is not erase acceptance. The product `instance_store`
helper now stops loading, awaits the actual WebKit/WebView2 deletion callback,
reads back empty cookies, and destroys the disposable window with a bounded
terminal registry check. Failure also permanently retires that window.
The probe calls this same helper and holds an actual HTTP cookie response until
after retirement. The corrected macOS debug probe passed; Windows and signed
package behavior are separate acceptance tiers. Previous failed logs and test
homes are retained as evidence.

Native HTTP uses the existing reqwest client with redirects/proxies disabled.
The standard `cookie` and `cookie_store` crates retain cookie semantics rather
than using a custom parser; `keyring` supplies platform credential storage.
`uuid` identifies the native profile and `tempfile` atomically writes only the
nonsecret connection record. Their dependencies are explicit in Cargo manifests.

## Actual B2/R1 enrollment boundary

`desktop/runtime-host/tests/test_enrollment_producers.py` joins the real built
B2 routes, Cloudflare HTTP adapter and isolated fake provider with the real R1
prepare/pair handlers and private journal/config persistence. The transport maps
only the canonical backend socket; response loss/delay and a mismatched outbound
digest are deliberate faults, never fabricated backend responses. Connector
startup is stubbed so no tunnel or local service is started.

Create separate clean, detached B2 and R1 test checkouts at the reported heads.
Build B2 with a disposable HOME/XDG environment and no `.env` files. Run from
the R1 checkout with its existing test environment:

```sh
DESKTOP_B2_CHECKOUT=<absolute-built-B2-checkout> \
DESKTOP_B2_HEAD=<exact-B2-commit> DESKTOP_R1_HEAD=<exact-R1-commit> \
PYTHONPATH=<absolute-R1-checkout> <R1-python> -m pytest -p tests.conftest \
  --rootdir <absolute-R1-checkout> \
  <absolute-S1-checkout>/desktop/runtime-host/tests/test_enrollment_producers.py -q
```

The test starts and terminates only its own B2 process, uses test-owned cookies
and homes, and checks both producer/consumer source heads. Five cases cover A2
pending/exact replay and all four A3 requirements: expired-unused renewal,
consumed/missing receipt, digest mismatch, and two actual old expiry replies
delivered around a lost replacement reply. UTF-8 device `小明的 Mac` is preserved.
The first run found a test adapter using the wrong config port field; it never
reached the held provider path. The corrected setup asserts the real Runtime
origin before testing pending and all five cases passed. The failed log is
retained. This is enrollment boundary evidence, not the full native/UI chain;
rerun with the final coordinated candidate heads after peer reviews settle.

## Actual native and bundled UI chain

`desktop/src-tauri/examples/instance_client_probe.rs` assembles the production
native coordinator, command, and disposable window owner with private in-memory
credentials. It embeds the immutable U1 account bundle. The Python fixture under
`desktop/src-tauri/tests/instance_chain/` runs one combined B1/B2 Next server and
two separate real R1 Runtime data roots. Native HTTP retains the canonical
protocol origins while mapping only approved sockets to loopback. WebKit uses a
fixture CONNECT proxy and accepts only the exact ephemeral certificate hash;
the fixture does not install a certificate or touch Keychain.

The current prewire inputs are B1 `690445a02491a2aeb4f73b66eef5a23a45000b00`,
B2 `b50add69a8429b94e1315b44884b0e5fdb4eaf1a`, R1
`9fe7aa63fb15d52c35a65236fa77f1ec20f44e34`, and U1
`f22da3cd5178529ca588e5ff8242ffaf88b4908a`. B2 still has review repairs pending.
These are working-path evidence, not the final coordinated integration gate.
The combined backend is a separate test-only checkout; no peer feature commits
are included in this branch.

Build the probe from this checkout after extracting U1's account artifact into
the ignored `desktop/dist` directory:

```sh
cargo build --manifest-path desktop/Cargo.toml -p avibe-desktop \
  --example instance_client_probe --locked --offline \
  --features tauri/custom-protocol,tauri/macos-proxy
```

From the isolated R1 checkout, set `DESKTOP_COMBINED_CHECKOUT` and
`DESKTOP_COMBINED_HEAD` to the built, immutable combined backend checkout;
`DESKTOP_R1_HEAD` to its exact Runtime head; `DESKTOP_RUNTIME_UI` to the extracted
U1 Runtime `ui/dist`; and `DESKTOP_NATIVE_PROBE` to the executable above.
`PYTHONPATH` must include the R1 checkout and this checkout's
`desktop/src-tauri/tests/instance_chain` directory. Run:

```sh
<R1-python> -m pytest -p tests.conftest --rootdir <R1-checkout> \
  <S1-checkout>/desktop/src-tauri/tests/instance_chain/test_native_chain.py -q -s
```

The first complete window run passed account form submission, real session
establishment, local prepare/ensure/pair, two-instance discovery, structured
consent, real OIDC token/JWKS verification, native cookie projection, remote
Workbench loading, remote IPC refusal, and the actual local row returning to
the local Workbench. Device names include `小明的 Mac` and `工作室 Studio`.
The external connector is unavailable by construction, but the actual first-pair
start owner records durable remote-access intent. No tunnel is started.

Earlier failed runs exposed last-window destruction requesting application exit,
cookie readback representing absent boolean attributes as `false`, and opaque
custom-scheme origins in page-load matching. Those product boundaries are fixed
and covered by this actual path. Separate fixture failures (decompressed HTTP
body with a stale gzip header, and clicking the refresh button instead of the
local instance row) are retained with their original logs. The chain still needs
logout, generation failure, and the real footer updater decision cases before
the final integration claim.

Runtime-host tests and native all-targets Clippy pass at this checkpoint.
Cross-checking the Windows target locally stopped before application compilation
because this macOS host lacks Windows C SDK headers required by `ring`.
Windows CI remains required; this local limitation is not a platform pass.

### Current native failure-boundary checkpoint (2026-11-13 05:00 +08)

The immutable test assembly is `226adca03d2f3aeb5bbe7af2403ad1b97b8fa37b`
at `/tmp/avibe-desktop-phase1-seszkwvjjwaz5/combined-backend-cc23881`:
B2 `cc23881ff2b2d31c2d4004e9117a968de9912e6e` plus the unchanged complete B1
`690445a02491a2aeb4f73b66eef5a23a45000b00` delta. The sole assembly conflict
was the CI job list; both candidate test groups are retained. No peer worktree
was changed. The build used a temporary HOME/XDG/TMPDIR and no real env file.
R1 and both U1 asset pins remain those listed above.

The current actual native chain has two passing cases (17.90 seconds): actual
U1 account login, registration, catalog, structured consent, OIDC, remote store
and local return; then a second remote selection followed by direct sign-out.
Native credential count and disposable instance-store count reach zero while
R1's local pairing and requested remote-access intent remain. A separate real
TOTP challenge blocks enrollment until verification; one deliberately incorrect
native CSRF header is rejected before local prepare changes any descriptor or
B2 device count. An actual B1 session response held at the native transport is
released after sign-out and rejected as `stale_generation` without recreating
account state, credentials or stores. Evidence is
`native-chain-cc23881-run2.log`; run1 is preserved (the fixture incorrectly
assumed an instance display name rather than using its actual catalog tuple).
The same assembly also passes all five actual B2/R1 A2/A3 cases in
`b2-cc23881-r1-a3.log`.

Two coordinator regressions first failed on the previous product implementation:
post-registration discovery discarded one-time recovery codes, and a cancelled
credential write hid a failed rollback behind `stale_generation`. Both are fixed
at their owner boundary; eight coordinator tests pass. A rollback error remains
visible to the cancelling request and retains erasure ownership until sign-out.
A native retirement error now still attempts local-window recovery and removes
the disposable directory, while returning the original failure. Changed Rust
fmt and native all-target clippy pass; changed Python Ruff passes.

These checks do not complete updater confirmation, download presentation,
Windows native execution, final quiescent integration, or signed-package gates.
The current probe was rebuilt from the checkpoint's source before the two-case
run. Earlier b50 chain results and the initial cookie erasure failure remain
historical evidence and are not substituted for this candidate.

### Updater and download candidate under validation

The production presentation URL handler now has one shared native caller check
used by the shell and the actual-window fixture. Trusted updater confirmation
carries its client generation through download and checks it again immediately
before the existing verified installer owner. A stale completion returns to
Available without an install or a new error dialog. Ordinary startup/menu and
remote-update mailbox ownership remain with the existing updater.

Windows native downloads are guarded at WebView2 DownloadStarting rather than
Tauri's on_download (the latter marks the event Handled and suppresses the
existing native UI). Only the current committed document can start a matching
origin download. Invalidation cancels its operations and closes their native
presentation. This is awaiting native Windows CI/consumer verification; local
macOS cannot supply the Windows SDK. macOS has no configured download handler
in the existing shell, and this change does not add one.

The runnable updater fixture build is
`desktop/src-tauri/tests/instance_chain/build_native_probe.py`. It archives the
specified native commit into a separate test-only directory, embeds the pinned
U1 account archive, and records binary/source hashes. Its only source adaptation
is the updater HTTP client's exact fixture proxy and certificate trust; the
build uses the existing repository public test key. Production key configuration,
signing files, user trust stores and installation paths are untouched. Discovery,
manifest verification, the actual native three-choice dialog and the generation
checks execute the candidate implementation. The signed artifact is the existing
38-byte inert fixture, not an application package. This is a native presentation
and cancellation probe, not release/package acceptance.

The updater fixture's first immutable run exposed a bootstrap crash: the
presentation caller evaluated Wry's current URL before initial navigation had
created one. The shared handler now reads it lazily, only for an enumerated
presentation URL. The failed native log remains preserved. A separate existing
restore test was expanded to cover credential erasure failure: it failed because
automatic sign-out discarded that failure. Automatic sign-out now propagates
cleanup failure, retains cleanup ownership and refuses a new account operation
until erasure succeeds. Eight coordinator tests pass after the fix; the
before/after logs are `s1-auto-signout-before.log` and
`s1-auto-signout-after.log` in the lane evidence directory.
