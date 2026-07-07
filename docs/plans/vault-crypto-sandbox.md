# Cross-origin protected Vault crypto sandbox

Status: design plan only, no implementation.
Owner: Vaults workstream.
Date: 2026-07-07.

## Background verified from current code

Protected Vaults have two tiers. Standard secrets are handled by the existing
`avault` envelope paths. Protected secrets are browser-side custody: the browser
holds a Vault Master Key (VMK), wraps that VMK with WebAuthn-PRF passkey copies
in `wrap_meta`, seals protected records, signs protected keypair requests, and
releases protected DEKs as HPKE blind boxes.

The current protected client crypto is concentrated in `ui/src/lib/vaultCrypto.ts`
and `ui/src/lib/useProtectedVault.ts`:

- `WrapMeta = { v: 1, copies: PasskeyPrfCopy[] }`; each passkey copy can unwrap
  the VMK independently. The `copies` list should remain, even while the first
  product surface supports one passkey, because it is the right extension point
  for later factors or multiple passkeys.
- Protected setup and unlock are passkey-only. The password-derived factor has
  been removed.
- The VMK is raw bytes cached in module-scope browser memory, refreshed on use,
  and zeroed on lock or auto-lock. Auto-lock is wall-clock enforced for 10
  minutes. Manual lock is broadcast across same-origin tabs.
- The code explicitly notes that there is no cross-origin sandbox yet.
- `webauthnAvailable()` rejects raw IP contexts such as `127.0.0.1`, so protected
  setup/unlock only works on `localhost` or HTTPS domain access today.

The daemon side is already aligned with browser custody. `storage/vault_protected.py`
is a reference/test path, while production protected decryption is browser-side.
`storage/vault_service.py` stores ciphertext, nonce, and opaque `wrap_meta`; it
does not receive the VMK, PRF output, DEKs, passkey secrets, or plaintext. That
daemon custody boundary must remain true.

The protected delete authorization plan in
`docs/plans/vault-protected-delete-authz.md` adds server-verifiable WebAuthn
assertions for protected deletion. This sandbox becomes the browser ceremony
surface for that assertion, but this document does not redesign the delete
authorization service.

## Product goal

Move protected Vault crypto into a constant-origin, cross-origin iframe hosted at
`https://sandbox.avibe.bot`, and make the main Avibe web app talk to it only
through a narrow typed `postMessage` RPC. The sandbox owns passkey ceremonies,
VMK unwrap, VMK lifetime, seal/open operations, protected signing, DEK release,
and protected-operation assertions.

The hard invariant is:

**The VMK, PRF output, raw DEKs, private keys, and plaintext values never appear
in any message crossing between the main app and the sandbox.**

Only operation results may cross: status, sealed envelopes, signatures, blind
boxes, WebAuthn assertion payloads, and structured errors.

## The two problems this solves

### 1. Same-origin XSS can currently read the VMK

Today the VMK lives in the same origin and JavaScript realm as the whole Avibe
web UI. A same-origin XSS can read module-scope `sessionVault.vmk` directly or
patch callers before they zero sensitive buffers.

Putting all VMK handling in `sandbox.avibe.bot` changes the failure mode. Main
app JavaScript, including main-app XSS, cannot script into the iframe's realm or
read sandbox memory. A main-app compromise may still try to invoke allowed RPC
operations, so the RPC surface must stay narrow and context-bound, but direct VMK
exfiltration is removed.

### 2. WebAuthn RP ID is fragile today

Passkeys are scoped to a WebAuthn RP ID, which is based on the ceremony's host.
Today that host is whichever origin the user opened the main app from:
`localhost`, a tunnel host, or a raw IP. That causes two user-facing failures:

- a passkey created on one host is not available on another host;
- raw IP access cannot run WebAuthn at all.

If every passkey ceremony happens inside `https://sandbox.avibe.bot`, then the
RP ID is always `sandbox.avibe.bot`, regardless of whether the parent app is
opened from `localhost`, `127.0.0.1`, or a user's `*.avibe.bot` tunnel address.

## Recommended architecture

Serve a minimal static sandbox app at `https://sandbox.avibe.bot`.

The main Avibe app embeds:

```html
<iframe
  src="https://sandbox.avibe.bot/v/<sandbox-version>/index.html"
  allow="publickey-credentials-get; publickey-credentials-create; clipboard-write"
></iframe>
```

The parent page also sends a Permissions-Policy response that delegates WebAuthn
only to the sandbox origin, while keeping first-party clipboard writes and
delegating clipboard writes to the sandbox:

```http
Permissions-Policy:
  publickey-credentials-get=("https://sandbox.avibe.bot"),
  publickey-credentials-create=("https://sandbox.avibe.bot"),
  clipboard-write=(self "https://sandbox.avibe.bot")
```

Do not include `self` in the WebAuthn directives. If the parent origin can run
WebAuthn, a same-origin main-app XSS could run the PRF ceremony against stored
`wrap_meta` and unwrap the VMK outside the sandbox.
This asymmetry is intentional: `clipboard-write` may keep `self` because the
existing first-party app already has copy flows, including backend OAuth setup
(`ui/src/components/settings/BackendOAuthPanel.tsx:317`) and workbench editor
copy actions (`ui/src/components/workbench/MonacoEditor.tsx:185`), while WebAuthn must
not.

The iframe is not just a hidden crypto worker. WebAuthn `create()` in a
cross-origin iframe requires transient user activation, and protected value entry
must not cross from the main app to the sandbox. The sandbox therefore needs a
small visible UI mode for setup, unlock, protected secret entry, protected
plaintext display/copy, and high-risk authorization prompts. The main app owns
navigation, metadata, daemon HTTP calls, and layout; the sandbox owns sensitive
inputs and sensitive crypto state.

The main app still brokers daemon HTTP calls. It fetches opaque `wrap_meta` and
protected envelopes from the daemon, sends only non-secret envelopes and
operation contexts to the sandbox, receives safe results, and relays those
results back to the daemon.

## Serve-location decision

### Recommended: A. central `sandbox.avibe.bot` plus local-anchored integrity

Use one central static site on the existing Avibe control-plane infrastructure.
There is no per-user tunnel domain, no per-user route, and no per-user sandbox
certificate. The RP ID is one constant host: `sandbox.avibe.bot`.

This is the right product shape because it solves both target problems at once:

- the sandbox has a different origin from the main app, so main-app JS cannot
  read VMK memory;
- passkeys are created and asserted under one stable RP, so the same protected
  vault can be unlocked from localhost, raw-IP parent pages, and tunnel pages.

The central site must be treated as static code distribution, not as a secrets
service. It never receives VMKs, PRF outputs, plaintext, DEKs, private keys, or
user vault contents. The remaining trust question is whether the central site can
serve malicious sandbox JavaScript. That is handled by the local-anchored
integrity model below.

**Decision (2026-07-07): A chosen over per-user (B), on scale cost.** Per-user was
re-evaluated and *can* technically deliver a stable per-user RP plus locally-served
sandbox code (which would remove the central-serve trust question and the hash pin
entirely — a genuine plus). It was rejected because it is an **O(2×users)**
Cloudflare hostname footprint: it halves whatever per-user ceiling applies — notably
the **1,000 routes/account Cloudflare Tunnel cap** — and doubles per-user hostname
cost (Cloudflare-for-SaaS custom hostnames are 100 free, then ~$0.10/hostname/mo),
whereas central is **O(1)** — one hostname, forever, free. The central-serve trust
is accepted and bounded by the local-anchored integrity model below. Possible future
refinement: ship central by default and let self-hosters point the parent at their
own sandbox origin.

### Rejected: B. per-user sandbox (e.g. `<user>-sandbox.avibe.bot`)

A single-label sibling host such as `<user>-sandbox.avibe.bot` is covered by the
existing `*.avibe.bot` wildcard cert and needs only one extra tunnel **ingress rule**
plus a DNS record per user (NOT a second tunnel) — and it isolates correctly from the
main app (a sibling host is not a registrable suffix of `<user>.avibe.bot`). It even
gives a stable per-user RP (the iframe src is a constant per-user host regardless of
localhost/tunnel access). The blocker is **scale cost**, not feasibility: O(2×users)
hostnames vs central's O(1) (see the Decision note above).

### Rejected: same host, different port

A different port gives DOM and JavaScript origin isolation because web origins
include the port. It does not give a stable unified WebAuthn RP, because RP IDs
are host-based and port-agnostic. `localhost:5123` and `localhost:<other>` still
produce the `localhost` RP, while a tunnel host produces a different RP. It also
keeps the non-443 fragility that many networks and embedded browsers block.

## 1. Origin and RP model

`sandbox.avibe.bot` is one shared WebAuthn RP. That is standard multi-tenant
WebAuthn: the RP is shared, while credentials are per user or per local install.
The shared origin does not imply shared credentials.

Design consequences:

- The sandbox passes `rp.id = "sandbox.avibe.bot"` for both registration and
  assertion.
- The credential user handle must become a per-vault or per-install random
  identifier, not the current hardcoded `"avibe-vault"` handle. The local daemon
  can mint and store a non-secret protected-vault user handle, and the sandbox
  uses it in WebAuthn creation options.
- `wrap_meta` should record `rp_id: "sandbox.avibe.bot"` for clarity and future
  diagnostics. Pre-launch timing means there are no production passkeys to
  migrate.
- Credential IDs remain stored in the passkey copies and, once #818 lands, in
  the daemon's protected WebAuthn authorization factor table.

Because all ceremonies run in the iframe, the browser binds the passkey to
`sandbox.avibe.bot` even if the visible parent page is:

- `http://localhost:<port>`;
- `http://127.0.0.1:<port>`;
- `https://<user>.avibe.bot`;
- another Avibe-controlled parent origin explicitly allowed by policy.

The raw-IP parent can be allowed to embed the sandbox. It could not run WebAuthn
itself, but it does not need to; the WebAuthn ceremony happens in the HTTPS
sandbox frame.

## 2. postMessage RPC protocol

The RPC boundary is the security perimeter. It should be a small discriminated
union with runtime validation on both sides. No caller should pass arbitrary
objects through to crypto helpers.

Common envelope:

```ts
type RpcRequest = {
  channel: "avibe.vault.crypto";
  version: 1;
  id: string;
  op: SandboxOperation;
  payload: unknown;
};

type RpcSuccess<T> = {
  channel: "avibe.vault.crypto";
  version: 1;
  id: string;
  ok: true;
  result: T;
};

type RpcFailure = {
  channel: "avibe.vault.crypto";
  version: 1;
  id: string;
  ok: false;
  error: {
    code: string;
    message?: string;
    retryable?: boolean;
  };
};
```

Common rules:

- The parent only trusts messages where `event.origin === "https://sandbox.avibe.bot"`
  and `event.source === iframe.contentWindow`.
- The sandbox only accepts messages from an allowed parent origin and pins the
  first accepted parent origin for that frame session.
- Parent origins allowed by policy should be limited to local development origins
  and Avibe-controlled tunnel origins: `http://localhost:*`,
  `http://127.0.0.1:*`, `http://[::1]:*`, and `https://*.avibe.bot`.
- Every request has an unguessable request ID, a timeout, and exactly one
  terminal response.
- Responses never include stack traces, raw exception objects, VMKs, PRF outputs,
  DEKs, private keys, or plaintext.
- WebAuthn operations use longer timeouts because user presence may take time;
  pure crypto operations should be short.
- The parent treats timeout as operation failure and may retry only operations
  documented as idempotent. Passkey creation is not blindly retried after an
  ambiguous browser result.

Lifecycle:

1. Sandbox loads and posts `ready`:

```ts
type Ready = {
  type: "ready";
  channel: "avibe.vault.crypto";
  version: 1;
  build: {
    sandboxVersion: string;
    buildHash: string;
    manifestHash: string;
  };
  capabilities: {
    webauthnGet: boolean;
    webauthnCreate: boolean;
    prf: "required";
    operations: SandboxOperation[];
  };
};
```

2. Parent verifies origin, source window, pinned build metadata, and local policy.
3. Parent sends `handshake`:

```ts
type HandshakeRequest = {
  expectedBuildHash: string;
  parentOrigin: string;
  appVersion: string;
  nonce: string;
};
```

4. Sandbox validates `event.origin === parentOrigin`, checks the origin allowlist,
   stores the parent origin and nonce, and returns:

```ts
type HandshakeResult = {
  accepted: true;
  sandboxOrigin: "https://sandbox.avibe.bot";
  buildHash: string;
};
```

Operations:

- `status`
  - Request: `{ wrapMeta?: string }`
  - Result: `{ state: "needs-setup" | "locked" | "unlocked"; expiresAt?: number; freshSetup?: boolean }`
  - Notes: `wrapMeta` is opaque encrypted metadata. It may be cached, but it does
    not authorize exposing the VMK.

- `setup`
  - Request:

```ts
type SetupRequest = {
  vaultUserHandle: string;
  displayName: string;
  existingProtectedVault: boolean;
  authzCreationOptions?: PublicKeyCredentialCreationOptionsJSON;
};
```

  - Result:

```ts
type SetupResult = {
  wrapMeta: string;
  rpId: "sandbox.avibe.bot";
  credentialId: string;
  authzRegistration?: PublicKeyCredentialJSON;
  state: "unlocked";
  expiresAt: number;
};
```

  - Notes: the sandbox creates the passkey, asserts it once to obtain PRF output,
    creates a fresh VMK, builds `wrap_meta`, holds the VMK in sandbox memory, and
    returns only the opaque wrapped metadata and optional server-verifiable
    registration response for #818.

- `unlock`
  - Request: `{ wrapMeta: string }`
  - Result: `{ state: "unlocked"; rpId: "sandbox.avibe.bot"; expiresAt: number }`
  - Notes: the sandbox parses passkey copies, runs `navigator.credentials.get()`,
    unwraps the VMK, and stores it only in sandbox memory.

- `lock`
  - Request: `{}`
  - Result: `{ state: "locked" }`
  - Notes: zeroes VMK and broadcasts lock to other sandbox-origin frames.

- `seal`
  - Request:

```ts
type SealRequest = {
  name: string;
  kind: "static" | "keypair";
  inputMode: "sandbox-entry";
  wrapMeta?: string;
};
```

  - Result:

```ts
type SealResult = {
  envelope: ProtectedRecordEnvelope;
  establishingVmk: boolean;
};
```

  - Notes: the apparent `seal(value)` operation must not send `value` over
    `postMessage`. The protected value is entered into a sandbox-owned field or
    generated inside the sandbox. The main app passes only non-secret context
    such as name and kind, and receives the sealed envelope.

- `unseal`
  - Request:

```ts
type UnsealRequest = {
  material: { name: string; envelope: ProtectedRecordEnvelope };
  mode: "sandbox-display" | "sandbox-copy";
};
```

  - Result: `{ completed: true }`
  - Notes: if protected plaintext must be shown or copied for a human, the
    sandbox renders it or writes it to the clipboard from the sandbox frame.
    Cross-origin clipboard copy requires `clipboard-write` delegation in both the
    parent Permissions-Policy and iframe `allow`. The plaintext does not return
    to the parent.

- `sign`
  - Request:

```ts
type VerifiableSigningContext =
  | {
      kind: "evm-transaction";
      chainId: string;
      unsignedTransaction: unknown;
      digestAlgorithm: "keccak256";
      digest: string;
    }
  | {
      kind: "eip-712-typed-data";
      typedData: unknown;
      digestAlgorithm: "eip712";
      digest: string;
    }
  | {
      kind: "avault-agent-operation";
      canonicalPreimage: string;
      digestAlgorithm: "avault-operation-hash-v1";
      digest: string;
    };

type SignRequest = {
  material: { name: string; envelope: ProtectedRecordEnvelope };
  scheme:
    | "ecdsa-secp256k1-recoverable"
    | "ecdsa-secp256k1-der"
    | "schnorr-secp256k1-bip340";
  signingContext: VerifiableSigningContext;
};
```

  - Result: `SignatureResult`
  - Notes: protected keys must not do blind digest signing. The request must carry
    typed transaction or canonical preimage data that the sandbox can
    independently hash and compare with `signingContext.digest` before rendering
    a human-readable confirmation. The sandbox must render only from data it can
    independently derive from the verified preimage, transaction, or typed-data
    payload; it must not accept parent-supplied display strings. If the sandbox
    cannot validate the preimage-to-digest binding or decode that context type
    with its own trusted decoder, it refuses to sign. After per-operation
    OS-backed confirmation (see "High-risk operations require top-level or
    OS-backed confirmation"), the sandbox opens the sealed private key under the
    VMK, signs the verified digest, zeroes the private key, and returns only the
    public signature. Full decode coverage is scheme-specific and should be
    phased; unsupported schemes fail closed rather than falling back to raw hex
    or parent-rendered approval.

- `releaseDEK`
  - Request:

```ts
type DaemonSignedAgentBinding = {
  challengeId: string;
  requestId: string;
  grantId: string;
  agent: {
    publicKey: AvaultPublicKey;
    fingerprint: string;
  };
  context: ProtectedDekDeliveryBlindBoxContext;
  expiresAt: string;
  signature: {
    alg: "ed25519";
    keyId: string;
    value: string;
  };
};

type ReleaseDekRequest = {
  material: { name: string; envelope: ProtectedRecordEnvelope };
  agentBinding: DaemonSignedAgentBinding;
};
```

  - Result: `BlindBox`
  - Notes: the sandbox must not accept a raw parent-supplied HPKE public key. The
    parent relays a daemon-signed binding over the grant/request id, operation
    context, agent public key, fingerprint, and expiry. The sandbox verifies that
    signature with the daemon verification public key pinned directly in
    VMK-authenticated protected-vault root metadata before unwrapping the DEK.
    If the design uses a fingerprint instead, the resolver must be
    parent-independent and trusted by the sandbox. The sandbox must never obtain
    the daemon verification key over parent `postMessage`. Only after verifying
    the binding does it seal the DEK to the authenticated resident agent key.
    The raw DEK never leaves the sandbox, and a compromised parent cannot
    substitute an attacker HPKE key.

- `deleteAuthzAssertion`
  - Request:

```ts
type DeleteAuthzAssertionRequest = {
  challengeId: string;
  operation: "delete_secret";
  secretName: string;
  webauthn: PublicKeyCredentialRequestOptionsJSON;
};
```

  - Result:

```ts
type DeleteAuthzAssertionResult = {
  challengeId: string;
  assertion: PublicKeyCredentialJSON;
};
```

  - Notes: this composes with #818. It is a server-verifiable WebAuthn assertion
    and does not require the VMK to be unlocked.

### High-risk operations require top-level or OS-backed confirmation

Isolation alone stops a main-app XSS from *reading* the VMK, but not from
*using* it: while the vault is unlocked, main-app XSS could send `sign`,
`releaseDEK`, or `unseal` RPCs and receive signatures, blind boxes, or plaintext
without the user's intent. For a signing keypair (crypto wallet) that is nearly
as damaging as key theft — an attacker who can request an arbitrary signature
during the unlock window can drain funds.

The sandbox therefore doubles as a **trusted confirmation surface**, not just a
key store. A bare in-frame button click is not enough, because a malicious parent
controls iframe size, position, opacity, stacking, and surrounding UI. Geometry
checks are necessary but not sufficient: a parent can still overlay benign UI
above a full-size visible iframe, and the OS WebAuthn prompt proves origin and
presence rather than the transaction text the user saw.

For the highest-risk operations, especially protected `sign` and `releaseDEK`,
the safe target is a top-level `https://sandbox.avibe.bot` confirmation context
using the same popup/redirect model as the WebAuthn-create fallback. A top-level
sandbox document gives the browser an unobscured top document and avoids parent
overlay control. When the product chooses in-iframe confirmation for lower
friction, the residual UI-redress risk must be treated as accepted, documented,
and limited by sandbox-derived display, visibility checks, and a fresh OS-native
WebAuthn user-verification assertion over an operation challenge.

- **`sign` requires per-operation confirmation, every time.** The unlock window
  releases the VMK into sandbox memory but does NOT pre-authorize signing. Each
  `sign` RPC must provide a verifiable signing context. The sandbox recomputes
  the digest from typed transaction/preimage data, refuses mismatches and
  unsupported blind-digest contexts, renders decoded human-readable details from
  its own trusted decoder, then requires top-level sandbox confirmation or a
  fresh WebAuthn `get()` user-verification assertion bound to that operation
  before it opens the key and signs. Main-app XSS can *request* a signature but
  cannot silently complete a verified signature ceremony.
- **`releaseDEK` requires in-sandbox confirmation** when it grants an agent
  ongoing access to a protected secret (the same confused-deputy-while-unlocked
  risk). The release is sealed only to a daemon-authenticated resident agent key,
  and the confirmation is bound to top-level sandbox confirmation or a fresh
  WebAuthn UV assertion. Headless protected DEK release must not be reachable
  straight from a parent RPC with a parent-provided key.
- **`unseal` to display/copy requires in-sandbox confirmation** and renders/copies
  only inside the sandbox frame; the plaintext never returns to the parent. Copy
  uses the delegated `clipboard-write` permission and should be gated by the same
  operation confirmation.

Before presenting any in-iframe high-risk confirmation, the sandbox should
enforce anti-clickjacking constraints:

- require the iframe to be fully visible, focused, and above a minimum size for
  the operation UI;
- use IntersectionObserver and viewport/geometry checks to refuse confirmation
  when the frame is clipped, hidden, tiny, or backgrounded;
- require the WebAuthn UV prompt to be triggered from sandbox-owned UI, not a
  parent-triggered hidden frame;
- fail closed if these checks cannot run.

These checks are necessary-not-sufficient. They block hidden/tiny/clipped
confirmations but do not defeat overlays above an otherwise visible iframe. Phase
0 must decide which operations require the top-level unobscurable sandbox
context versus best-effort iframe confirmation. Auto-lock remains a backstop that
bounds the window, but operation confirmation — not the unlock window — is the
authorization for each high-risk operation.

## 3. Integrity model: central serving, local-anchored trust

The local install is the user's trusted root. A centrally served sandbox must not
be a blind trust regression where a compromised static host can silently ship
VMK-exfiltrating crypto.

Important constraint: SRI does not apply to `<iframe src=...>` itself. Browser
enforced subresource integrity can protect scripts and styles inside the iframe,
but not the iframe document as a single navigated resource. A sandbox design that
only checks a self-reported build hash during handshake is weak because malicious
code can lie.

Compared mechanisms:

### Pinned loader plus SRI

A tiny loader HTML at the sandbox origin loads versioned JS/CSS assets with SRI
and a strict CSP. This is useful because a compromised asset store cannot swap
the active JS without breaking browser SRI. It is not sufficient alone unless the
loader document itself is also verified or otherwise immutable, because iframe
HTML has no native SRI.

### Parent fetches and hashes resources

The local main app fetches the sandbox manifest, loader HTML, and referenced
assets with CORS, computes SHA-256, and compares them with an expected manifest
baked into the local Avibe install. If any byte differs, protected Vault crypto
fails closed and the UI shows an upgrade/integrity error.

This gives the local install an enforceable pin before it enables the sandbox.
It also makes legitimate upgrades explicit: a new Avibe release updates the
pinned sandbox manifest and expects a new immutable sandbox version path.

Residual caveat: because browsers do not enforce integrity on iframe navigation,
a fully malicious server that can equivocate per request could serve honest bytes
to the parent's verifier and different bytes to the iframe. Under that same
equivocation, the server also controls its own response headers and could omit
`worker-src 'none'` while serving the iframe, allowing a malicious same-origin
Service Worker to persist. `worker-src 'none'` helps in the honest-serve case,
but the parent cannot enforce the sandbox CSP for an equivocated navigation.
The parent must at least navigate the iframe to the exact URL it fetched and
hashed; verifying `/index.html` while loading `/index.html?...` reopens the
equivocation gap as a separate byte request. This remains in the central-host
equivocation residual bucket, bounded operationally by immutable versioned URLs,
SRI inside the verified loader, reproducible builds, the sandbox `buildHash`
ready/handshake gate, public hash transparency, fail-closed telemetry, and a
future browser-supported iframe-integrity or signed-web-bundle mechanism if one
ships.

### Sandbox self-reports build hash

The sandbox should report its build hash in `ready` and `handshake` for
diagnostics and version gating. This is meaningful only after the parent has
verified the resources or when combined with browser-enforced SRI. It is not a
security boundary by itself.

### Recommendation

Use a combined model:

1. The Avibe package includes a pinned sandbox integrity manifest:

```json
{
  "sandboxOrigin": "https://sandbox.avibe.bot",
  "sandboxVersion": "2026.07.07",
  "resources": {
    "/v/2026.07.07/index.html": "sha256-...",
    "/v/2026.07.07/assets/sandbox.js": "sha256-...",
    "/v/2026.07.07/assets/sandbox.css": "sha256-..."
  }
}
```

2. The parent fetches and hashes the manifest, loader, and assets before enabling
   the iframe. Static sandbox responses must set CORS headers allowing this
   verification from local and Avibe tunnel origins.
3. The loader uses SRI for JS/CSS and a CSP that allows only those same assets.
4. The iframe URL is versioned and immutable. No mutable `latest` path is used
   for protected crypto.
5. Honest sandbox responses include `worker-src 'none'`; Service Workers remain
   out of the reviewed integrity model unless a later design deliberately adds
   them. This is not parent-enforceable under central-host equivocation.
6. The sandbox reports the same build hash during handshake; the parent treats a
   mismatch as fail-closed diagnostic evidence, not as the primary proof.
7. Legitimate sandbox upgrades ship with a new Avibe local install version that
   pins the new manifest. Old local installs continue to use their old immutable
   sandbox version until they upgrade.
8. Avibe publishes the sandbox source, reproducible build instructions, and the
   hash manifest so the served bundle is independently verifiable.

This preserves the local-first trust story: the VMK never leaves the browser,
and the trusted local install decides which central static bytes may handle it.

## 4. WebAuthn inside a cross-origin iframe

WebAuthn is disabled in cross-origin iframes by default. The parent must delegate
both WebAuthn features:

- `publickey-credentials-get` for `navigator.credentials.get()`;
- `publickey-credentials-create` for `navigator.credentials.create()`.

Delegation is needed in both places:

- parent HTTP `Permissions-Policy`;
- iframe `allow` attribute.

The protected copy path also needs `clipboard-write` delegation in both places.
Chromium blocks Clipboard API writes from cross-origin iframes unless that
feature is explicitly granted. Unlike WebAuthn, `clipboard-write` should keep
`self` or be scoped to Vault pages only, because existing first-party UI flows
copy OAuth and editor values from the top-level app.

The sandbox must trigger WebAuthn from a user action inside the iframe because
cross-origin credential creation requires transient activation. The main app can
request a setup/unlock operation, but the sandbox should render the button or
confirmation step that actually calls WebAuthn.

Browser support caveat:

- Current WebAuthn Level 3 guidance allows WebAuthn in cross-origin iframes when
  delegated with the feature tokens.
- MDN still marks the Permissions-Policy directives as limited/experimental, so
  this must be verified across Avibe's target browser matrix before shipping.
- Cross-origin `get()` has broader historical support than cross-origin
  `create()`. `create()` is the riskier registration path and requires transient
  activation.
- Safari support needs particular attention. As of the current public guidance,
  Safari does not expose `topOrigin` in client data, which affects server-side
  verification context. CSP `frame-ancestors` and sandbox origin allow-listing
  remain required even when `topOrigin` is unavailable.

Fallback:

- If `create()` fails in the iframe because the browser does not support
  cross-origin creation, open a top-level `https://sandbox.avibe.bot` setup
  window. It keeps the same RP ID, collects protected value input inside the
  sandbox origin, and returns only `wrap_meta`, sealed envelopes, or authz
  registration payloads to the opener.
- If `get()` fails in the iframe, use the same top-level sandbox window for the
  specific operation. UX is worse, but the security invariant and stable RP hold.
- Do not fall back to same-origin main-app WebAuthn for protected crypto; that
  reintroduces both target problems.

## 5. CSP and frame policy

Sandbox response headers should be strict:

```http
Content-Security-Policy:
  default-src 'none';
  script-src 'self';
  style-src 'self';
  img-src 'self' data:;
  font-src 'self';
  connect-src 'none';
  worker-src 'none';
  object-src 'none';
  base-uri 'none';
  form-action 'none';
  frame-ancestors
    http://localhost:*
    http://127.0.0.1:*
    http://[::1]:*
    https://*.avibe.bot;
```

The exact CSP must match the final asset strategy. If the loader uses inline
boot code or hashed styles, prefer hashes over broad inline allowances.
Service workers are explicitly blocked with `worker-src 'none'` in honest
sandbox responses; they are out of the reviewed integrity model unless a later
design adds them deliberately. Under central-host equivocation, the parent cannot
force this header onto the actual iframe response, so Service Worker persistence
remains part of the equivocation residual risk rather than a fully solved issue.

Allow `127.0.0.1` to embed the sandbox. The raw-IP parent still cannot be a
WebAuthn RP, but it no longer needs to be. The sandbox's HTTPS origin is the RP.

The main app should restrict child frames:

```http
Content-Security-Policy:
  frame-src 'self' https://sandbox.avibe.bot;
  child-src 'self' https://sandbox.avibe.bot;
```

Do not make this a global exclusive `frame-src https://sandbox.avibe.bot`
policy. The existing UI already embeds first-party frames, including Show Pages
from the workbench chat page and same-origin file previews. Either preserve
`'self'` alongside the sandbox origin, or scope the tighter policy to Vault pages
only. The main app should not allow arbitrary frames to receive WebAuthn or
clipboard delegation. Use an exact sandbox origin in Permissions-Policy and in
the iframe `src`.

## 6. Auto-lock, cross-tab lock, and sandbox storage

Move the VMK lifecycle from `useProtectedVault.ts` into the sandbox:

- VMK is memory-only in the sandbox realm.
- VMK is zeroed on manual lock, auto-lock, frame unload, setup collision discard,
  and any fatal crypto/session error.
- Auto-lock remains 10 minutes and wall-clock enforced. Every crypto operation
  checks the deadline before using the VMK.
- Manual lock broadcasts over a `BroadcastChannel` owned by the sandbox origin,
  scoped by a non-secret install or vault id, for example
  `avibe-vault-lock:v1:<installId>`.
- Broadcast messages contain no secrets. They are lock/status signals only, and
  still include the non-secret install/vault id so unrelated protected-vault
  iframes on the shared `sandbox.avibe.bot` origin ignore them.
- Browser storage and BroadcastChannel are partitioned by top-level site, so this
  channel is best-effort within one top-level-site partition. A sandbox iframe
  under `http://localhost` should not be expected to signal a sandbox iframe
  under `https://<user>.avibe.bot`.
- For cross-site "lock everywhere", use a daemon-mediated non-secret monotonic
  lock generation. The sandbox checks that generation through the parent-brokered
  daemon API before using the VMK and after focus/resume; if the daemon generation
  is newer than the sandbox's local generation, the sandbox zeroes the VMK and
  reports locked. The daemon is the shared authority across storage partitions.
- Auto-lock can remain per iframe. A background tab expiring should not force an
  active tab to lock unless the product explicitly chooses global idle lock.

The sandbox may cache only non-secret metadata:

- current `wrap_meta` string;
- passkey credential IDs;
- PRF salts from `wrap_meta`;
- the non-secret per-vault user handle;
- lock status and wall-clock expiry.

Prefer memory for all of the above. If session storage is needed for reload UX,
limit it to `wrap_meta`, credential IDs, salts, and user handle. Never persist
VMK bytes, PRF output, raw DEKs, private keys, or plaintext.

Unlock sharing across tabs is intentionally not required for the first design.
Each iframe can require its own passkey unlock. A later optimization could use a
sandbox-origin SharedWorker, but that should be a separate security review.

## 7. Daemon and `wrap_meta`

The daemon remains mostly unchanged:

- It stores protected `ciphertext`, `nonce`, and opaque `wrap_meta`.
- It never unwraps the VMK.
- It never receives PRF output, VMK bytes, raw DEKs, private keys, or plaintext.
- It still enforces the atomic first-init guard through `establishing_vmk`.
- It still expires pending requests and grants when protected envelopes rotate.

Expected client-visible `wrap_meta` changes:

- `rp_id` should become `sandbox.avibe.bot`.
- A non-secret per-vault user handle or key ID may be added for shared-RP
  WebAuthn account separation.
- The daemon verification public key itself must be pinned in VMK-authenticated
  protected-vault root metadata so `releaseDEK` can verify daemon-signed
  agent-key bindings without trusting parent-supplied key material. A
  cryptographic fingerprint is acceptable only if the sandbox has a
  parent-independent trusted resolver. A bare `keyId` resolved through parent
  data is not sufficient.
- The existing `copies` list should remain the factor extension point.

Minimal daemon changes may be needed to expose or store non-secret sandbox
metadata, such as the protected-vault user handle and the pinned sandbox version
expected by the packaged UI. Those are not crypto custody changes.

The main app remains the HTTP broker. It reads daemon data, sends opaque
envelopes and operation context to the sandbox, then sends sandbox results back
to daemon APIs.

## 8. Composition with protected delete authorization (#818)

#818 requires a fresh server-verifiable WebAuthn assertion before deleting a
protected row. With this sandbox:

- the delete assertion is produced inside `sandbox.avibe.bot`;
- the RP ID is stable and no longer depends on localhost vs tunnel access;
- the same passkey ceremony surface handles unlock, sign, DEK release, and
  delete authorization;
- the daemon still verifies the delete proof and still performs the final
  fail-closed storage mutation.

The delete assertion does not need the VMK. It should be a separate
`deleteAuthzAssertion(challenge)` RPC operation that receives daemon-issued
request options and returns the WebAuthn assertion JSON for the parent to submit.

Setup should be designed so the passkey registration can also register the
server-verifiable authorization factor required by #818. That means preserving
the WebAuthn create response and credential public key path, while still using
the PRF assertion result to wrap the VMK. This simplifies #818 because there is
one RP and one ceremony surface.

## 9. Cross-repo infrastructure dependency

Serving `https://sandbox.avibe.bot` is a control-plane dependency, likely in
`avibe-bot-backend` and the avibe.bot domain infrastructure. This `avibe` repo
plan drives the client refactor but does not implement the central static site.

Infra requirements:

- static, versioned, immutable paths under `sandbox.avibe.bot`;
- HTTPS with HSTS;
- CORS headers that let local and Avibe tunnel parents fetch resources for
  integrity verification;
- strict CSP including `frame-ancestors`;
- `worker-src 'none'` unless a later reviewed design deliberately adds Service
  Workers to the integrity model;
- no dynamic user data, cookies, sessions, or APIs;
- published reproducible build hashes;
- release process that ties a sandbox version/hash manifest to a local Avibe
  package version.

## 10. Threat model

Defends:

- Main-app XSS reading the VMK directly. The VMK is in a cross-origin iframe
  realm that parent JS cannot inspect.
- Main-app XSS reading protected plaintext or raw key material directly, once
  protected value entry/display also moves into the sandbox.
- WebAuthn RP fragmentation between localhost, tunnel hosts, and raw IP parent
  pages.
- The raw-IP WebAuthn dead end, because the ceremony runs on the HTTPS sandbox
  origin.
- A malicious parent outside the allowed origin set embedding the sandbox. It is
  blocked by `frame-ancestors` and sandbox-side origin checks.
- Accidental central static asset drift, because the local install pins and
  verifies the expected sandbox bundle.

Does not fully defend:

- A compromised local install. The user trusts their local Avibe install; it
  controls the pinned manifest and daemon broker.
- A fully compromised sandbox bundle. The integrity pin is the mitigation; if a
  malicious bundle is accepted by the local pin, it can exfiltrate secrets.
- Browser or passkey-provider compromise.
- Malware that can drive the user's browser and satisfy OS/passkey prompts.
- Main-app XSS invoking allowed sandbox RPC operations while the vault is
  unlocked. Direct key exfiltration is removed by isolation; abuse of high-risk
  operations is blocked only when the sandbox validates the operation context,
  derives its own display from verified inputs, and binds approval to top-level
  sandbox confirmation or a fresh WebAuthn UV prompt. A bare in-frame click is
  clickjackable and is not a security boundary. `sign` is confirmed per operation
  every time; `releaseDEK` authenticates the resident agent key before sealing;
  `unseal`-to-plaintext stays sandbox-owned. Lower-risk operations stay
  context-bound; delete/authz stays server-challenge-bound. Residual: a
  compromised parent can still try to trick a *present* user, and in-iframe
  confirmation remains overlay-redressable even with geometry checks. Highest-risk
  operations should escalate to top-level sandbox confirmation unless Phase 0
  explicitly accepts iframe residual risk.
- A compromised parent substituting an attacker HPKE key for protected DEK
  release. The sandbox refuses raw parent-supplied agent keys and verifies a
  daemon-signed agent-key binding against the daemon verification public key
  pinned directly in VMK-authenticated root metadata before unwrapping and
  sealing the DEK. The verification key is never accepted over parent
  `postMessage`.
- A central host that can perfectly equivocate between the parent's verification
  fetches and the iframe navigation. Current web platform support lacks native
  iframe SRI. Immutable versioned URLs, loader SRI, CSP, reproducible builds,
  public hash transparency, and fail-closed telemetry bound this risk
  operationally. `worker-src 'none'` is honest-serve defense-in-depth only: a
  host that equivocates can omit it on the iframe response and register a
  persistent Service Worker, so Service Worker persistence remains part of this
  residual rather than solved by the parent. Future iframe integrity or
  signed-web-bundle support should be adopted if available.

Residual trust statement:

Avibe's central infrastructure serves code, not secrets. The trusted local
install decides which central code hash is allowed to run protected crypto. The
design is a substantial improvement over same-origin VMK custody, but it is not
hardware attestation for arbitrary web content.

## 11. Rollout and migration

This is the right time to do the refactor because protected Vault passkeys have
not launched broadly. There are no existing production passkeys bound to
localhost or tunnel hosts that need migration. After launch, changing the RP ID
would strand credentials or require a complex re-enrollment flow.

This is still a real multi-week architecture project. It rewires working
passkey-only client crypto into an isolated origin, adds a security-critical RPC
boundary, adds browser compatibility handling, and introduces a cross-repo
static hosting dependency.

### Phase 0: design acceptance and support matrix

**Browser-feasibility spike — DONE (2026-07-07, real iOS device).** The iframe
architecture is viable on iOS Safari with a **setup-vs-ops split**:
- same-origin WebAuthn-PRF works on iOS (iCloud Keychain / 1Password);
- cross-origin iframe **`create()` is BLOCKED by Safari** ("origin of the document
  is not the same as its ancestors") for any isolated RP → **passkey SETUP must run
  top-level** (popup or full-page redirect) on the sandbox origin;
- cross-origin iframe **`get()` + PRF works** → **daily ops (unlock / sign /
  releaseDEK / delete-authz) run in the cross-origin iframe**.
This setup-in-top-level + ops-in-iframe split is intrinsic to real isolation on
Safari (only the insecure apex-RP option avoids it); it is the standard iframe-
sandbox pattern and is no longer an open architectural risk.

- Confirm the exact parent origins Avibe will support.
- ~~Verify cross-origin `get()`/`create()` on Safari/iOS~~ — **done (above)**;
  still worth a wider provider/browser matrix pass before GA.
- Decide the exact top-level sandbox **setup** UX (popup vs full-page redirect;
  redirect is smoother on mobile / dodges popup blockers). This is the primary
  setup path now, not a fallback.
- Decide whether direct protected plaintext display/copy is in scope for the
  first sandbox release or whether first release only supports create, sign, and
  DEK release.
- Top-level vs iframe confirmation policy for high-risk operations. **Decided:**
  `sign` requires per-operation confirmation every time (no unlock-window
  pre-authorization), with sandbox-verified preimage/transaction context before
  signing; `releaseDEK` and `unseal`-to-plaintext require operation confirmation
  as well. Phase 0 must decide which operations require a top-level unobscurable
  sandbox context versus best-effort iframe confirmation, and finalize
  scheme-specific decode coverage plus visibility/geometry requirements.
- Finalize the local pinned manifest format and release process.

### Phase 1: static sandbox app and RPC skeleton

- Add a minimal sandbox app build target.
- Implement typed `postMessage` handshake, request IDs, timeouts, origin checks,
  and structured errors.
- Add parent-side sandbox client wrapper and readiness/status UI.
- Add no-op capability detection without moving crypto yet.
- Add tests for origin rejection, timeout handling, duplicate IDs, and malformed
  message rejection.

### Phase 2: move setup, unlock, lock, and VMK lifecycle

- Move passkey creation/assertion, `buildWrapMeta`, `unwrapVmk`, VMK memory,
  auto-lock, wall-clock enforcement, and BroadcastChannel lock into the sandbox.
- Change parent Vault unlock UI to render the sandbox ceremony surface.
- Preserve the atomic first-init flow: sandbox returns `establishingVmk`, daemon
  rejects split VMK history, parent tells sandbox to discard/refresh on collision.
- Remove same-origin VMK access from `useProtectedVault.ts`.

### Phase 3: move protected operations and sensitive UI

- Move protected secret value entry/generation into sandbox-owned UI for `seal`.
- Move protected plaintext display/copy into sandbox-owned UI for `unseal`, if
  that product surface remains supported.
- Move protected signing and DEK release into sandbox RPC operations.
- Build the high-risk confirmation UI:
  per-`sign` confirmation that verifies typed preimage/transaction data against
  the digest and renders decoded human-readable detail derived by the sandbox,
  plus `releaseDEK` / `unseal` confirmation. The highest-risk operations use the
  top-level sandbox context unless Phase 0 explicitly accepts iframe residual
  risk. The parent cannot render, prefill, dismiss, or auto-approve these
  prompts; a bare iframe click is not sufficient.
- Add daemon-signed agent-key binding verification before `releaseDEK` unwraps a
  DEK, using the daemon verification public key pinned directly in
  VMK-authenticated root metadata.
- Keep the parent as the daemon broker and result router only.
- Add focused crypto vector tests and RPC contract tests, including a test that a
  parent-issued `sign` cannot complete without a verified preimage binding,
  sandbox-derived display, and the required top-level or WebAuthn UV
  confirmation.

### Phase 4: integrity, CSP, and control-plane deploy

- Stand up immutable `sandbox.avibe.bot` static hosting in the control plane.
- Add CSP, frame-ancestors, CORS for verification fetches, cache-control, and
  HSTS.
- Add pinned sandbox manifest to the local Avibe package.
- Add parent fetch-and-hash verification before enabling the iframe.
- Add loader SRI and strict asset paths.
- Add `worker-src 'none'`, exact WebAuthn/clipboard delegation to
  `sandbox.avibe.bot`, and main-app frame policy that preserves existing
  first-party frames.
- Publish reproducible build instructions and hash manifest.

### Phase 5: protected delete authorization seam

- Route #818 WebAuthn registration and delete assertion ceremonies through the
  sandbox.
- Keep daemon challenge issuance and assertion verification in the daemon/API
  layer.
- Ensure delete authz can run while the VMK is locked, because delete does not
  need VMK access.
- Add tests proving protected delete uses `sandbox.avibe.bot` as RP and that the
  daemon still fails closed without verified authz.

### Phase 6: hardening and regression

- Run browser/provider compatibility labs for cross-origin PRF, `create()`,
  `get()`, and popup fallback.
- Run Avibe's normal UI build and focused Vault crypto tests.
- Exercise localhost, raw-IP parent, and tunnel parent access in regression.
- Add security review specifically for the RPC allowlist, plaintext boundary,
  integrity assumptions, and confused-deputy risks.

## References checked for browser behavior

- [WebAuthn Level 3](https://www.w3.org/TR/webauthn-3/): public key
  credentials are scoped to a WebAuthn Relying Party and can only be accessed by
  origins belonging to that RP.
- [MDN `publickey-credentials-create`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Permissions-Policy/publickey-credentials-create):
  the Permissions-Policy directive controls `navigator.credentials.create()`
  with `publicKey`, with default allowlist `self`.
- [MDN `publickey-credentials-get`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Permissions-Policy/publickey-credentials-get):
  the Permissions-Policy directive controls `navigator.credentials.get()` with
  `publicKey`.
- [web.dev passkeys within iframes](https://web.dev/articles/webauthn-within-iframe):
  parent Permissions-Policy, iframe `allow`, CSP `frame-ancestors`, secure
  `postMessage()`, and cross-origin context validation are the defense-in-depth
  pieces; Safari lacks `topOrigin` support as of May 2026.
