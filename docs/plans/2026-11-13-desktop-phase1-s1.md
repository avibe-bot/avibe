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
