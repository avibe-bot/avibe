# Protected Vault passkeys in the iOS App

Status: decided by the owner on 2026-11-11 (see Decisions). The device check
is still open.
Date: 2026-11-10.
Related: `ios-app.md` section 7 ("Known risk: Protected Vault passkeys"),
`vault-crypto-sandbox.md`, `vault-sandbox-protocol-v2.md`,
`vault-sandbox-integrity.md`, and the vault-sandbox repository.

## Goal

In the iOS App, a protected-tier Vault secret works end to end: creating the
protected Vault passkey, unlocking, and approving a use (run, fetch, sign,
reveal). The system passkey sheet comes up natively. Protected-tier guarantees
do not weaken, enrolled passkeys keep working, and the relying-party id stays
`sandbox.avibe.bot`.

## What happens today

Evidence is marked **read** (code or a live fetch on 2026-11-10), **cited**
(Apple or WebKit source), or **not observed** (nobody has recorded a device
run; the iOS coordination Session has no record of one either).

### Which surface each flow uses

The App adds nothing Vault-specific. It runs the same Web flow as Safari:

| Flow | Where the ceremony runs | Surface in the App |
| --- | --- | --- |
| Setup, step 1: `create()` | top-level sandbox window, because WebKit rejects `create()` in a cross-origin iframe (`AuthenticatorCoordinator.cpp`: "The origin of the document is not the same as its ancestors") | child `WKWebView` in a form sheet |
| Setup, step 2: PRF `get()` | sandbox iframe in the Workbench | primary `WKWebView` |
| Unlock (R1, locked) | sandbox iframe | primary `WKWebView` |
| Approve access, reveal (R2) | top-level sandbox authorization window; passkey only when locked or Strict | child `WKWebView` in a form sheet |
| Sign (R3) | top-level sandbox authorization window; passkey always | child `WKWebView` in a form sheet |

**Read:** `vault-sandbox/src/main.ts` (`handleSetup`, `handleUnlock`, the
`mode=authorize` window), `ui/src/lib/vaultSandboxClient.ts:182` (the parent
reserves an `about:blank` window in the approval click),
`ios/Avibe/WebViewController.swift:1176` (`presentChildWebView`: a
`UINavigationController` form sheet showing the host, Done, and Open in
Safari). No `SFSafariViewController` or `ASWebAuthenticationSession` is
involved. The Web UI hands Vault steps to the browser only in the desktop
shell (`ui/src/lib/vaultBrowserHandoff.ts`); the iOS shell is not covered.

So the "in-app page" is the sandbox's own top-level setup or authorization
window, which protocol v2 makes the only surface that may collect an R2/R3
consent (`vault-sandbox-protocol-v2.md` section 6.6). It is a security
surface, and it is separate from how the passkey is invoked.

### Why the passkey fails

1. **Cited:** in a third-party app, WKWebView offers passkeys only when the
   relying-party id is one of the app's `webcredentials` associated domains
   (Apple engineer, developer forums thread 726943). WebKit's UI-process code
   runs inside the app and calls
   `ASAuthorizationPlatformPublicKeyCredentialProvider(relyingPartyIdentifier: rpId)`
   itself (`WebAuthenticatorCoordinatorProxy.mm`, `requestsForAssertion` and
   `requestsForRegistration`), so the system applies the association check to
   `bot.avibe.ios`.
2. **Read:** the App signs exactly `webcredentials:avibe.bot`
   (`ios/Avibe/Avibe.entitlements`; `ios/scripts/testflight.py` enforces the
   exact set). The Vault passkey's relying party is `sandbox.avibe.bot`
   (`vault-sandbox/src/webauthn.ts`, `rpId()` is the page hostname). An
   association does not extend to subdomains.
3. **Read:** `https://sandbox.avibe.bot/.well-known/apple-app-site-association`
   answers 404, at the host and at Apple's CDN
   (`app-site-association.cdn-apple.com/a/v1/sandbox.avibe.bot`). `avibe.bot`
   serves `{"webcredentials":{"apps":["68B2SQ6YK3.bot.avibe.ios"]}}`.

Every Vault ceremony in the App therefore asks the system for a relying party
the App is not associated with, and the system refuses before any passkey sheet
appears. WebKit maps that refusal to a `NotAllowedError`
(`WebAuthenticatorCoordinatorProxy.mm`, the `ASAuthorizationErrorFailed`
branch), which the sandbox reads as a cancellation or a generic failure.
**Not observed:** the exact text the user sees.

### Platform facts that shape the options

- **Cited:** PRF needs iOS 18. `HAVE(WEB_AUTHN_PRF_API)` gates it in WebKit,
  and the native `ASAuthorizationPublicKeyCredentialPRFAssertionInput` is
  iOS 18.0+. Safari, WKWebView, and the native API share this floor. On
  iOS 17 the protected tier cannot work in any surface.
- **Cited:** WKWebView's WebAuthn and a native `ASAuthorizationController`
  call are the same system API, called from the same process, under the same
  association check, showing the same system sheet. "Native" and "in the web
  view" differ in who starts the ceremony and who receives the result, not in
  what the passkey sheet looks like.
- **Read:** the profile already grants `associated-domains = *` and the App ID
  has the capability, so adding a domain needs no developer-portal change.
- **Read:** an instance page cannot ask for this passkey itself, with or
  without the association. `sandbox.avibe.bot` is not a suffix of an instance
  host, so WebKit falls back to related-origins validation
  (`/.well-known/webauthn` on the sandbox), which does not exist and must not
  be added.

## Recommendation: associate the App with the sandbox host

Add the missing platform association and change nothing else.

- **vault-sandbox:** serve
  `/.well-known/apple-app-site-association` with
  `{"webcredentials":{"apps":["68B2SQ6YK3.bot.avibe.ios"]}}` and
  `Content-Type: application/json`. Copy it into `dist/` in a step after
  `release:assemble`, from a file outside `src/` and outside
  `scripts/release*.mjs`, so the release identity, the retained bytes, and the
  Avibe pin are untouched. Merging deploys Production, so it needs the owner's
  authorization.
- **App:** add `webcredentials:sandbox.avibe.bot` to
  `ios/Avibe/Avibe.entitlements`; add it to `MAIN_REQUIRED_ENTITLEMENTS` in
  `ios/scripts/testflight.py` and its test; update `ios/README.md` and
  `ios-app.md` section 7. No Swift change.
- **Web UI, daemon, sandbox runtime, pin:** no change.
- **Order:** deploy the association file first, then install the new build.
  iOS reads the file through Apple's CDN when the app is installed or updated.

### What the user sees

An Agent needs a protected secret while he is in the App:

1. The request card appears in the chat. He taps Approve.
2. A sheet rises showing the sandbox's authorization card (session, command,
   destination, duration, the secrets). He taps Confirm.
3. If the Vault is locked, Strict is on, or the request is a signature, the
   system passkey sheet appears and he confirms with Face ID. Inside the
   unlock window an access approval needs no passkey.
4. The sheet closes and the Agent continues.

First-time setup: Create passkey in the form opens the sheet, the system
sheet saves the passkey, the sheet closes, and Finish asks for one more Face
ID to derive the key. Unlock is one tap and one Face ID with no sheet.

This is the Safari flow, working. Step 2 remains: the system sheet is native,
but he still passes through the sandbox's page.

### Minimum iOS version

Protected tier in the App needs iOS 18.0. The App's deployment target (17.0,
17.5 proposed elsewhere) does not change.

### Trust change

None in the Vault model. The ceremony still runs in sandbox code, and the PRF
output, the Vault Master Key, and plaintext stay in the sandbox origin; the
instance page and the App's own Swift code see nothing new. What is new is one
statement: the sandbox host names `bot.avibe.ios` as an app allowed to use
passkeys for `sandbox.avibe.bot`, the right Safari has by being the browser.
The App already hosts the sandbox in its web view and, like any browser, is
trusted to run it unmodified. The association file is static JSON served
outside the pinned release and carries no code.

### To verify on a device

One TestFlight build answers all of these. They are not observed yet:

1. `create()` with PRF and `get()` with PRF in the top-level sandbox sheet.
2. PRF `get()` in the sandbox iframe inside the Workbench. WebKit marks this
   request cross-origin with the instance as top origin; whether the system
   accepts that from a non-browser app is the one real unknown.
3. The sheet's result reaches the requesting iframe through `window.opener`.
4. Setup, unlock, approve (locked and unlocked), sign, and reveal each
   complete, with an iCloud Keychain passkey enrolled earlier in Safari and
   with one created in the App.

If item 2 fails, unlock and setup's second step move into the top-level
window the sandbox already has. That is a sandbox release, a new pin, and an
Avibe release (about 2 to 3 days), with no trust change.

### Estimate

About half a day for each of the two PRs. Elapsed time is set by review, the
owner's authorization of the sandbox deploy, Max's agreement, a TestFlight
build (about 10 minutes), and Apple's CDN picking up the file.

## Alternatives

### B. Native passkey bridge to the sandbox

The App registers a message handler that answers only frames whose origin is
`https://sandbox.avibe.bot`. The sandbox calls it instead of
`navigator.credentials`; the App runs `ASAuthorizationController` with the PRF
input and returns the assertion and PRF output to that frame.

- **User sees:** the same as the recommendation for approvals, because the
  sheet exists for the consent review, not for WebAuthn. Setup loses its sheet,
  since the native API has no cross-origin-iframe rule for `create()`.
- **Changes:** sandbox protocol and a new release, pin, and Avibe release; a
  Swift bridge, its origin rule, and tests (Max). It needs the same
  association file and entitlement as the recommendation.
- **Minimum iOS:** 18.0.
- **Trust change:** the App's own Swift code receives the PRF output, which
  with the wrap metadata the instance page already holds unwraps the Vault
  Master Key. No new party is trusted, since the App can already script any
  frame it hosts, but handling that secret becomes the App's designed job. The
  public, reproducible sandbox stops being the only code that touches it, and
  `ios/` joins the reviewed protected-tier code. The instance page sees
  nothing new. It cannot be reduced to nothing while native code performs the
  PRF ceremony.
- **Rejected:** a second mechanism for what the platform association already
  provides, for a gain limited to setup.

### C. Native approval sheet

As B, and the App also draws the review natively from details the sandbox
sends after verifying the daemon's signature.

- **User sees:** Approve, a native sheet with the details, Face ID.
- **Changes:** B, plus a display contract and a native copy of every review
  the sandbox renders (access, reveal, EVM, typed data, Bitcoin).
- **Minimum iOS:** 18.0.
- **Trust change:** B's, and the review surface moves from the public sandbox
  into the App binary.
- **Rejected:** the largest change, and it duplicates the code the protected
  tier depends on most. About 2 to 3 weeks plus a security review.

### D. Passkey straight from the request card

Drop the sandbox sheet in the App: Approve brings up Face ID directly, through
the iframe or bridge B.

- **User sees:** Approve, Face ID. This is the literal request.
- **Changes:** sandbox protocol v2 section 6.6 for the App, a new release and
  pin; B if native.
- **Minimum iOS:** 18.0.
- **Trust change:** the passkey sheet names the relying party, not the
  operation, so he would approve what the instance page shows. That page is
  the origin Agent-authored Show Pages share. A compromised page could show
  one request and submit another genuine pending one: a different secret
  released to the Agent, or a different signature. The sandbox-rendered review
  exists to close exactly this. This weakens the protected tier and is the
  owner's decision.
- **Not recommended.**

### E. System browser (Safari or `ASWebAuthenticationSession`)

Open the Vaults page in Safari's context, where the passkey works without any
association, as the desktop shell does today.

- **User sees:** leaves the App or gets a system sign-in sheet, signs in to
  the instance again the first time, approves there, comes back.
- **Changes:** Web UI only for Safari (extend `vaultPasskeyNeedsBrowser`);
  App work for `ASWebAuthenticationSession`.
- **Minimum iOS:** 18.0.
- **Trust change:** none.
- **Rejected:** the opposite of the request. It remains the fallback if the
  device check fails outright.

## Possible follow-ups, not part of this change

- **A sheet that feels native.** The App could present sandbox windows as a
  bottom sheet with a fixed title instead of the generic browser child (host,
  Done, Open in Safari). App-only, about a day, no trust change. It makes
  Protected Vault a shell-specific presentation, which `ios-app.md` section 7
  currently rules out, so it needs Max's agreement. Decide after seeing the
  recommendation on a device.
- **One tap when a passkey is required anyway.** The authorization window
  could start the passkey as it opens, so Face ID replaces the Confirm tap
  while the sandbox's details stay visible behind it. It changes every
  browser, needs a sandbox release and pin, and depends on WebKit's
  user-gesture rule. It needs its own design.

## Consistency with passkey sign-in

Account sign-in uses a different passkey under `avibe.bot`, already
associated. This adds a second `webcredentials` domain to the same
entitlement and exact-set check; neither passkey can stand in for the other.

## Decisions

Owner, 2026-11-11:

1. The recommendation is adopted: association only. The sandbox review sheet
   stays, and alternative D is off.
2. Deploying the association file to `sandbox.avibe.bot` is authorized
   (avibe-bot/vault-sandbox#27).
3. The App change is made without waiting for Max, who is told afterwards
   (#3506).

No "needs iOS 18" message is added for now; revisit after the device check.

Order: the association file is deployed and visible at Apple's CDN, then the
App change merges and a TestFlight build is cut, then the device check above.
