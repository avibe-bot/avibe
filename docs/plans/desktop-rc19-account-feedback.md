# Desktop account feedback: A4 and A5

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
retirement that leaves an old WebView alive remains fenced. Its cleanup
obligation is retained, and it cannot regain command authority or credentials.

## A5: one physical main and retained documents

The shell keeps the actual Workbench WebView while showing the owned account
child. Back reveals that document, retaining route, session, scroll and drafts.
Unique `main-account-*` and `main-runtime-*` labels identify roles only; actual
physical incarnation, document, origin and generation checks confer authority.
Physical hide/reopen retains the context; destruction and account reload retire
it. Account commands receive the invoking WebView, and their capability has no
physical-window selector. Full account DOM events check the exact URL and
ephemeral per-document nonce at execution. Runtime receives only minimal state.

Target selection first loads a separately stored hidden candidate. Load failure
or cancellation preserves the retained source. The existing transaction then
awaits old-store retirement, close and candidate show/focus, persists credentials
and connection metadata, and publishes the role/target and successful-only MRU.
The old fire-and-forget completion and cookie/URL restoration snapshot are gone.
If terminal retirement or persistence fails, the old store is never revived:
rollback and the existing safe-local cleanup owner converge to a trusted error
receiver, with explicit retry and no MRU. OS refusal remains a blocking cleanup
obligation. There is no promise of restoring a document after retirement starts.

Native owns successful authentication/enrollment return, using its initiating
action owner. It requires persisted signed-in plus paired-this-account state.
Restore/get-state, failed/incomplete enrollment and newly issued recovery codes
remain in the owned UI. An explicit recovery acknowledgement uses ordinary
trusted Cancel after the UI checks the current completed state.

`local.slug` joins the current validated local ID to the same-account catalog's
name. Unavailable proof or catalog match gives null. Full-only
`current_instance` preserves display metadata for the exact current remote
ID/origin/account when omitted from a later catalog; it is neither persisted
nor used for authorization. Older producers normalize missing fields to null.

Main controls use configured x12/y16 and the existing28pt clearance. The
physical-window macOS frame owner reapplies those coordinates after page/frame
and size/focus events, since retiring Wry's initial parent also retires its
inset redraw callback. Actual normal/minimum-size AppKit measurements own this
regression; Mini, other-window and Windows geometry are unchanged.

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
- A5 cases in that consumer cover local/remote retained document identity,
  held/cancelled and failed candidate navigation, terminal show/erase/persistence
  failure, successful-only recency, native auth return, recovery-code exclusion,
  reload/full-event privacy, physical destruction and main focus/close/resize.
  The archive-only shell assembly executes the product event and navigation
  owners with an inert RuntimeHost. OS notification/browser output is recorded
  at the final external-effect boundary. It does not open the user's browser.
- Existing bridge/presentation/locale tests: transient direct result and
  captured locale. Product view consumers are integrated by the UI owner.

Canonical auth scenario IDs remain owned by the existing auth setup catalog.
These tests extend the native owning layer; they do not pretend the Python
canonical harness executes a WebView. The final integrated package is built
from one exact source commit and gets separate artifact/signature and isolated
embedded-Runtime smoke evidence before any owner-authorized installation.
