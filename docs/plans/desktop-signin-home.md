# Return to the selected Workbench after Desktop sign-in

## Contract

Max requested this change on 2026-11-26 after a completed Desktop sign-in left
him on Account. This local implementation starts from explicitly fetched master
`ab1ff36d4b21b00551617077f60091b9a6f05f22`. No push, PR, packaging or installation
is authorized for this branch.

AccountApp defaults to `view=account`. Its signed-out branch renders the shared
sign-in UI; successful authentication removes that branch and reveals whichever
view was already selected. The Account page always has an Instances back button,
so that label does not establish whether the person signed out first. Sign-out
already returns to the local instance; it is not changed here.

After an ordinary completed email-code, password, second-factor, passkey or
provider sign-in, close the trusted account surface and navigate the currently
committed instance to `/`. Keep that instance: a selected remote stays remote;
local stays local. The existing Runtime's root route still owns any required
initial setup. Do not choose an instance from the refreshed catalog.

Before → after:

- Account/welcome/sidebar login: Account details → selected Workbench home.
- Login presented from Instances: instance list → selected Workbench home.
- Required authenticator enrollment: show the one-time recovery codes first;
  after acknowledgment, return home even when this computer is unbound.
- Ordinary cancellation continues to restore the previous document, not home.
- Already-signed-in Account/Instances visits remain management views.

Required follow-up stays visible: incomplete authentication, stale or failed
results, credential persistence failure, pending instance authorization or
switching, an unreachable selected instance, active binding/claim or adding an
instance, and unresolved binding registration/conflict/failure. The claim dialog
continues its own flow after sign-in and after its recovery-code acknowledgment.
The iOS implementation and shared login steps are unchanged.

## Ownership and race boundaries

AccountApp marks completion only after a successful direct native authentication
reply. A signed-in broadcast can precede a persistence failure, and enrollment
can return recovery codes after that broadcast; neither broadcast authorizes
navigation. Completion is tied to the reply's generation. Finishing enrollment
without new recovery codes can complete immediately; new codes require the
existing explicit acknowledgment. The existing synchronous recovery guards remain.

The new Desktop action `return_home` uses the existing escape owner, but requires
the current generation and a signed-in, usable committed target without pending
consent or a binding error. It does not switch instances or sign out. Explicit
restore destinations distinguish preserving the original document, returning to
its home, and returning local. Native checks the current document's origin against
the committed target before asking that WebView to navigate to its root, without
its old query/fragment. It first closes the account surface and retires its
return receipt under the existing native window lock. Home navigation refuses
a live Account receipt. If a newer Cancel/ReturnLocal wins before navigation,
the old callback cannot navigate; after navigation, a later Cancel restores the
current Runtime without consulting the replaced document. Existing restoration
and local-bootstrap handling remain authoritative. Root navigation is ordinary native
WebView navigation; its command acceptance is not proof that the server loaded.

## Validation

- AccountApp protocol fixtures cover direct-result ordering, stale/failed replies,
  required follow-up, and recovery-code protection/acknowledgment. The two new
  email-completion regressions fail on the base AccountApp because no home action
  is sent.
- Rust coordinator fixtures cover signed-out refusal, generation fencing, retained
  account/instance, remote preservation, and ordinary cancel's document destination.
  Native shell unit coverage checks local/remote home URL derivation, refusal to
  replace a document with a live Account receipt, stale pre-navigation ownership,
  and cancellation after navigation has changed the Runtime document.
- Production account assets run in isolated Chromium and WebKit against a fake
  native bridge. All egress is blocked except loopback static assets. These checks
  prove form-to-native-action wiring, not OS WebView focus or authenticated loading.
- Existing shared/iOS, binding, and sign-in suites provide regression coverage.
  Typechecks, lint and UI/Desktop frontend builds are required locally.
- Evidence and executable browser probe live in the main matter's
  `evidence/signin-home/`. Installed-app acceptance remains outstanding; no user
  window, account, credential, keychain or system authorization is exercised.
