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
U1 account archive, and records binary/source hashes. Source adaptations are the
updater HTTP client's exact fixture proxy and certificate trust, plus recording
automatic OS notification and popover nudges inside the fixture process. Explicit
CTA dispatch, native dialogs, remote mailbox ownership and installation gates
remain actual. The build uses the existing repository public test key. Production key configuration,
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

The full native suite found four existing inventory assertions still describing
the pre-client shell. They now enumerate the added local-only account capability
and command, and inspect the shared window builder used by both startup and
selection. The original popup, marker and capability restrictions remain checked.
The real footer probe also exposed its own observation navigation cancelling the
CTA's scheme navigation; the driver now observes native events after that click.
Pinned rfd uses a system-level CFUserNotification for an unparented macOS dialog.
Phase 1 trusted confirmation now attaches the existing three-choice dialog to
its current main window, so window retirement also owns its presentation. Legacy
startup/menu presentation is unchanged; the callback and download retain the
same generation fence. The fixture inspects only this process's modal/sheet.

### First review repair and immutable validation inputs

The first reviewed head is `5babee38cad5deebed62db5bc7786ab44a6b9e19`.
Its three findings concern startup restore continuation, AwaitingHost approval
ownership, and exact bundled bootstrap completion. Lead approved their repair
together with the same-target pending-view race. This remains one findings-bearing
review head; the repair is not yet an exact-head review pass.

Startup restoration can continue only after local lifecycle invalidation.
Cancel, ReturnLocal, sign-out, account replacement and failed erasure preserve
their fences. The startup owner retains its initial remembered target across
an interrupted credential read or remote selection. A table-driven regression
first failed with `stale_generation`; all nine coordinator tests now pass,
including the ten held-read/session lifecycle and explicit-cancellation cases.

View publication now requires the exact selection generation's commit receipt.
Per-window completion captures its generation because Tauri Webview equality
compares reused labels. Pending or staging views cannot present account pages
through page requests or native menu presentation. Exact owned bootstrap URLs
can finish offline escape without broadening remote navigation authority.

AwaitingHost confirmation retains the existing request, ticket and target.
The existing dispatcher expiry and claim transition remain authoritative.
Late download completion settles that exact remote request as failed and never
reaches the installer after generation invalidation. Ordinary footer confirmation
continues through the existing three-choice native dialog.

The actual held-window consumer exposed a native cookie restoration defect:
Wry 0.57.0 exports explicit false Secure/HttpOnly flags as Foundation string
properties, whose presence enables the flag. Projection omits false properties
only on macOS and verifies the full cookie tuple after the write. Windows retains
explicit boolean setters. The native cookie probe uses the same production
projection and retirement helpers, including a CSRF cookie read/project roundtrip.
Lead independently confirmed the Foundation behavior. The initial erase failure
and all failed full-chain logs are preserved.

Current immutable peer inputs are B1
`690445a02491a2aeb4f73b66eef5a23a45000b00`, B2
`510b36652ff046fb8e820645dcd8efaceb7bc8aa`, Runtime
`1f9870f0ccf6e7af922e0625246c85d4919274d0`, and U1
`f22da3cd5178529ca588e5ff8242ffaf88b4908a`.
The single-server backend assembly is
`8fc3608f0a2135d86c15b375d3827978692389c8`, based on B2 plus B1's original
delta; only their CI test lists required an assembly conflict resolution.
Lead independently passed all five actual B2/Runtime A2/A3 cases at these pins.
Earlier backend and Runtime pins above are historical checkpoints.

The reusable `desktop/src-tauri/tests/instance_chain/run_native_chain.py` accepts
explicit backend/Runtime heads, checkouts, Runtime UI directory, immutable native
binary and log path. It records the binary hash, command and retained isolated
HOME/XDG/TMP/Avibe state. Add `--updater` for the real footer and AwaitingHost
cases. Build that binary with the adjacent `build_native_probe.py` and the pinned
U1 account archive. Its provenance lists every fixture source adaptation.

The `8463ad1` native run passed eight of eleven cases. It exposed an actual
remote-ticket settlement omission after a fenced download, now repaired, and
two fixture waits that missed events already consumed before their terminal
reply. Those waits now observe the actual destination or preserve event order.
The corrected full eleven-case run and final cookie probe are pending at this
checkpoint. Rust fmt, all-target Clippy, native tests and changed Python Ruff
pass. Windows execution, signed-package acceptance, exact-head PR gates and
Lead's independent full-chain run remain required; no package or live service
was installed, signed or changed.

### Second review: receiving document and interrupted presentation

The reviewed head `2bd3305d97b019031ffdbbbf0b44c74d11c327e0` has two valid
findings. The lifecycle continuation class recurred across two reviewed heads,
so edits stopped for Lead diagnosis. Lead released the bounded repair in
`s1-2bd3305-disposition.md`; findings-bearing heads remain two. Independent red
consumers demonstrated account-only state reaching Runtime after rollback and
Cancel, and hidden local replacements after both SignOut and ReturnLocal.
Earlier passing eleven-case, cookie and startup results remain historical.

Persistent initialization now contains only the Runtime projection. Full account
events retain native caller checks and an exact receiving-document guard at
JavaScript execution time. The existing trusted U1 get_state supplies its full
initial account state. The fixture delays one actual WKWebView evaluation across
navigation through a test-only Objective-C method interceptor; it does not alter
the product event script or evaluation implementation.

The existing serialized window owner retains previous-view material and local
presentation intent until matching navigation and visibility complete. Only a
local lifecycle change can continue that owner under a new epoch; explicit user
actions supersede it. Production bootstrap navigation acquires the same window
lock, or binds its validated origin for the current replacement owner. Exact
selection receipts, per-window completion generations and store retirement
remain authoritative. No public protocol or peer asset changes are involved.

Added real consumers cover same/different-instance rollback, a delayed full event,
SignOut/ReturnLocal disappearance and rebind, explicit escape supersession, and a
second lifecycle change during rollback. These consumers are awaiting their first
immutable repaired-candidate run. Local compilation, changed Python Ruff and nine
existing coordinator consumers pass at this checkpoint; this is not a review or
integration acceptance claim.

The first repaired checkpoint exposed a different-instance rollback projection
that still named the failed candidate. The strengthened actual consumer failed
on the wrong instance ID/origin; rollback now resets its initialization projection
to current committed state before building. A local-response consumer also sent
its observer into a retired window; it now waits for the actual destination's
load event before evaluating. Both failed runs are retained separately.

A held-credential consumer additionally failed before the window owner was even
called: ReturnLocal was discarded by a lifecycle-only epoch check. Presentation
and its restoration-record write now share the accepted action owner; explicit
later actions supersede it. The consumer starts from a selected remote and reads
back the real ConnectionRecord after local return, including a late old Cancel
and refresh around a newer ReturnLocal. Ten coordinator tests pass. The existing
cleanup-failure case now covers the native store owner's retirement error: it
first demonstrated a prohibited new account HTTP request, then passed after that
error was retained by the existing cleanup owner. Successful sign-out retry clears
the error; superseded presentation does not conceal an erasure failure.

At product checkpoint `8626297ffc5a99f511959ca0e4ad6a8fdf7ebf43`, all nine new
actual-window consumers passed, followed by the full twenty-case chain including
the original eleven cases. The source-exact cookie probe passed once with strict
attribute/isolation/readback and terminal erasure checks. Workspace fmt,
all-target/all-feature Clippy, native tests and changed Python Ruff passed. The
startup continuation and cleanup negative controls are included in the native
suite; the remote-to-local record consumer reads the actual persisted record.

The new real-window lifecycle tests call the validated lifecycle binding directly;
they do not start the production bootstrap RuntimeHost. Production open_workbench
and restore_bootstrap_view use the same serialized window guard in their actual
source, and native compilation covers that glue. This distinction is retained in
the handoff rather than treating a fixture callback as a full installed-shell
bootstrap run. Actual update dialogs/download fencing and cookie retirement use
the unchanged production owners. Windows execution and the existing signed-package,
OS credential-store/passkey/associated-domain/live-account checks remain RC work.

This final documentation checkpoint leaves the tested product/test source bytes
unchanged. The PR body and immutable artifact provenance identify the pushed head,
replay commands, exact peer inputs and final logs. Fresh exact-head Codex review,
expected CI/CLEAN, zero all-head unresolved threads and Lead's independent real
chain remain mandatory before Merge-ready. Original lane Watches and historical
findings-bearing head count (two) remain unchanged.

The next CI run rejected the fixture-only `AtomicUsize::fetch_update` call as
deprecated on its newer Rust toolchain; local Rust 1.90 had accepted it. Both
native jobs stopped at the same warning before their tests. The lifecycle
interruption counter now uses the equivalent checked compare-exchange loop,
which preserves zero and consumes each requested interruption exactly once on
both toolchains. Product source and wire behavior are unchanged. Original CI
logs and the fixed candidate's complete twenty-case/cookie evidence are retained;
the repaired fixture must pass compilation and its actual lifecycle consumers
before the next candidate is handed off.

## Third review: production entry ownership

Lead dispositions `78edfd340709` and `414b18b7f90e` release a bounded repair
of the existing DeepLinks/NativeView admission and the shared local notification
connection. Findings-bearing heads remain three (5bab, 2bd, 4fc).

Frozen 4fc actual production callbacks reproduce repeated local replacement
from both remote and account presentation. Actual Runtime update POST creates
the request but times out awaiting the stopped Connection, with SSE and fallback
poll configurations. The first attempt's CSRF/schema fixture failures are kept
separately; the corrected red consumer uses `/api/csrf-token`, the actual isolated
SQLite schema, actual ASGI SSE and the test-owned updater mailbox/status.

DeepLinks now records one return attempt and the action receiving the input.
Busy document callbacks hand off to the serialized NativeView completion.
Native return uses the admission generation through the existing escape; escape
acceptance and state mutation share the inner lock. A controlled invalidate gap
reproduced an old return replacing a newer OTP state before this correction.
Eleven coordinator consumers, including durable credential-wait precedence,
and thirty notification consumers pass locally.

Selection leaves the monitor-owned Connection alive. Ordinary notification and
badge output capture a presentation generation before asynchronous reads and
recheck it on the native main thread. Updater nudges keep the existing local
connection generation, mailbox, confirmation and installation owner. The
fixture adds shared production receive/Finished callbacks and an inert Shell;
only final OS notification/badge/shortcut effects are intercepted. Installed
Runtime bootstrap is unavailable and would panic if invoked. All new real-window
consumers and fresh exact-head review/CI remain pending at this checkpoint.

The first immutable third-round checkpoint passed five actual receive/Finished
cases (remote, account, lifecycle interruption, newer link, newer explicit
escape), actual SSE/prompt delivery, and delayed ordinary-output/stop/rebind
checks. Its automatic-ticket test wrongly assumed ReturnLocal cancels the
existing automatic updater owner. It reached the installer, which rejected the
non-application-bundle executable before staging or external commands; no install
replacement occurred. That failure is retained. The corrected consumer expires
and reads back the same actual Runtime request before releasing the held inert
artifact, then requires native Expired and no Installing/Installed. Automatic
ticket authority and UI TrustedDecision generation are distinct existing owners;
no updater product code changes. Both builder and runner reject resolved binary
paths inside an `.app` bundle. The pre-install expiry and repeated different-port
rebind consumers pass on the same native checkpoint.

Hot link delivery now schedules on the native main thread and holds the existing
window guard through navigation/commit, so an async completion cannot race a
newer main-window delivery. The commit also checks the receiving action owner.
The fixed native suite passed 548 tests with three existing ignored cases,
including the original boundary assertions updated for the extracted callback.
The final candidate must run all 28 actual chain consumers (original 20 plus
8 production-entry cases), from its archived source, before handoff. The cookie
probe/helper, dependency lock and updater installer bytes are unchanged; their
independent 4fc cookie evidence remains scoped to that unchanged boundary.

## Pre-admission lifecycle continuation

Lead independently passed the c689a37 candidate's 28 actual-chain cases,
11 coordinator cases, 30 notification cases, exact-head review and native CI.
It then reproduced an uncovered interval: a link is claimed, local lifecycle
advances generation before the spawned return is accepted, and the old strict
generation check rejects it. Treating that rejection as a terminal failed
delivery strands the still-current link. This is an independent consumer
finding, not another findings-bearing Codex head; the historical count stays
three. The released scope is the same link/NativeView/coordinator ownership
boundary, without changes to updater policy, peers or the frozen wire.

Native return now supplies both the generation and action owner captured at
claim. The original escape acceptance lock checks that owner and rejects future
generations, while permitting intervening lifecycle-only changes. Acceptance
and state mutation remain atomic. The escape returns its actual accepted action
receipt, which DeepLinks uses for completion instead of inferring captured
generation plus one. Newer explicit actions still reject old work without state,
record or view effects. Cleanup/presentation failures remain terminal for the
same link; there is no retry loop or second queue.

The existing coordinator fixture now composes actual DeepLinks and Coordinator
for lifecycle once/twice, newer explicit action, newer link, and cleanup failure.
The lifecycle case fails on c689a37 and all 12 coordinator cases pass after the
correction. The archived native fixture adds a bounded scheduling pause after
the production claim and before the real coordinator call. It changes no
authority/result and is absent from the shipped library. Three real
receive/Finished controls cover repeated lifecycle changes, latest-link
precedence from account presentation, and a newer explicit escape. The frozen
c689a37 remote case fails to deliver the route; the explicit escape remains
valid. An initial newer-link assertion counted rejected and accepted attempts
together; it was narrowed to actual accepted-generation and delivery precedence,
and that fixture-only failure is retained separately.

Final validation uses the same immutable B1/B2 assembly, R1 and both U1 bundles.
Run the affected native controls before the original 28-case chain on one fixed
artifact, then fresh exact-head review and macOS/Windows CI. Unchanged
cookie/store, notification/updater and B2/R1 A2/A3 evidence retains its previous
scope. Independent Lead execution and final lane quiescence remain required.
