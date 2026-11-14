# Desktop account feedback: A4

## Behavior and boundaries

The account window supports password as a first factor through the same native
account session, TOTP/recovery and enrollment owners as OTP. Switching first
factor retires the old attempt, including its provider verifier and pending MFA
cookie. A failed replacement leaves coherent signed-out state on reload.
Provider errors are bounded codes; secrets are transient and redacted in Debug.

The local row uses validated `/api/desktop/local-instance` pairing facts,
independently of account enrollment or a selected remote Workbench. Current
unpaired, invalid or unavailable proof publishes null identity and URL.
Only remote access On publishes a canonical URL. Account publication and local
refresh share the coordinator's existing operation serializer, preventing an
old account clone from resurrecting failed local proof.

The guarded presentation handoff carries optional strict en/zh locale.
Workbench captures it before awaiting its guard. The bundled window applies it
before rendering; it does not read the other origin's localStorage.

An admitted sign-out presentation now requests direct logout. Native stores an
opaque one-use receipt bound to the actual source, action owner, generation,
physical window and navigation document. The matching document's `get_state`
direct result may repeat that unspent receipt. Automatic sign-out requires it;
consumption uses the existing SignOut cleanup owner. A bare URL, reload or
replay never creates a receipt. Explicit trusted UI controls may still issue
tokenless sign-out. Receipt possession binds execution, not a physical click.

If credential or store cleanup fails, the same owner records its error before
creating a fresh trusted sign-out receiver, retaining the admitted locale.
That receiver gets the error through ordinary `get_state`, with no new receipt;
only an explicit current retry can repeat cleanup. A failed OS window
retirement that leaves the old window alive remains fenced and cannot create
a second window or fall back to older credentials.

No backend route, persisted schema, identity owner, credential storage,
updater policy or OS passkey implementation is added. Existing TOTP/recovery
remain available. Installation, real credentials, TCC/Keychain continuity,
signed updates and installed Runtime handover are separate acceptance.

## Validation owners

- `runtime-host/tests/instance_client.rs`: strict URL grammar, exact local
  proof and legacy/additive protocol decoding; synthetic Transport.
- `runtime-host/tests/instance_coordinator.rs`: first-factor replacement,
  stale responses, cookie persistence, local failed-refresh publication and
  concurrency; synthetic Transport and credential store.
- `src-tauri/src/instance_client.rs` tests: guarded presentation queue and
  document-bound one-use receipt.
- `src-tauri/tests/instance_chain/test_account_feedback.py`: real native
  commands, actual WebView navigation and HTTP sockets through the existing
  isolated native probe. Provider/backend replies and credential storage are
  test-owned substitutes. This is not a live provider or installed-app claim.
- Existing bridge/presentation/locale tests: transient direct result and
  captured locale. Product view consumers are integrated by the UI owner.

Canonical auth scenario IDs remain owned by the existing auth setup catalog.
These tests extend the native owning layer; they do not pretend the Python
canonical harness executes a WebView. The final integrated package is built
from one exact source commit and gets separate artifact/signature and isolated
embedded-Runtime smoke evidence before any owner-authorized installation.
