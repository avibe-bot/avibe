# Protected Vault entry-address UX

## Change contract

An unsupported entry must be visible before a protected action. No signed
binding/reveal preparation, VMK discovery, sandbox prewarm, iframe, popup or
credential step starts there. The UI retains deny/cancel, public metadata and
standard-tier operations, subject to their existing permissions. The shared
client/window guard also rejects programmatic callers, using localized codes.

The parent-entry assessment is a precondition, not a passkey-support detector:

- Loopback `localhost`, `127.0.0.1`, `[::1]`, HTTP/HTTPS and any port.
- `avibe.bot` and its subdomains over HTTPS on the default port only.
- A secure browser context (including ancestors) and parent Web Crypto.
- Desktop shell keeps its existing browser handoff, not address remediation.

The address rule is the intersection of the pinned sandbox's RPC allowlist and
its hosting `frame-ancestors` policy (`vault-sandbox` source
`52b690a47f88f9eb29e3efa1d8ce706f7afc0b78`, `src/rpc.ts` and `vercel.json`).
RPC accepts non-default Avibe HTTPS ports but CSP does not. CSP's HTTP loopback
sources allow HTTPS upgrades and wildcard ports. This change does not alter
sandbox policy, the release pin, RP, permissions, authorization context or keys.
An allowed address does not establish browser/device WebAuthn or PRF support.

## Consumer inventory

| Entry | Early behavior |
| --- | --- |
| Access/sign approval (Vaults, chat and request dialogs share `VaultApprovalCard`) | Notice before Approve; no bindings, refresh or prewarm; deny remains usable |
| Setup/unlock (`VaultProtectedUnlock`) | Notice precedes checking/error/stale unlocked state; no ceremony/retry spinner |
| Create/provision (`VaultSecretForm`) | No protected refresh or submit/generate, even with stale unlocked state; Standard/cancel/deny remain available |
| Field edit (`VaultSecretFieldsEditor`) | No protected refresh/value write; metadata-only rename/removal remain separate |
| Reveal/copy (`VaultsPage` menu and desktop/browser deep links) | No signed reveal context/prewarm; disabled reveal/copy with notice; edit/delete remain available |
| Shared prewarm/refresh hook | Defensive gate before client acquisition/discovery |
| Client factory/singleton and synchronous popup preopen | Defensive gate before transport, iframe or popup; consumers catch/localize preopen failures |
| Lock/state appearance paths | Shared client gate prevents new transport; local lock/reset remains fail-closed |

## Remediation and navigation

One EN/ZH notice distinguishes an unsupported address from a browser-context
capability failure. Only address failures offer Remote Access remediation.
Owners get a React Router Link to `/settings/remote-access`, using the existing
Settings continuation state on mobile and desktop. Other users are asked to
contact the instance owner; they never get an owner-only settings link.
Navigation does not enable, pair, redirect to a remote entry, or write settings.
URLs carry neither secret drafts nor approval payloads.

Settings retains the Vaults route and restores the open approval/dialog. The
existing shared `DialogContent` suspends its contents when the route is inactive,
so form inputs are cleared, not persisted. The create/edit notice discloses this
before the Settings link. Retaining secret drafts or changing shared modal
lifecycle is deliberately out of scope.

On the computer running Avibe, loopback is an alternative. A phone's loopback
address is not the Avibe host. HTTP LAN and arbitrary HTTPS domains are not
supported protected-Vault parents. An allowed address with unavailable browser
capabilities gets direct, compatible-browser advice instead of an enable prompt.

## Validation

`AUTH-SETUP-137` is a bounded approval-consumer contract: three named, collected
Vitest cases prove EN/ZH early advice, blocked protected preparation (including
unhydrated protected metadata), usable deny, owner-only remediation and unchanged
standard signing. Both Python citation-shape validation and JavaScript/Vitest
case resolution apply. The row does not claim a whole browser continuation from
these consumer-only citations.

The supplementary offline closed loop remains in the existing
`ui/e2e/vault-integrity` Playwright harness. It uses actual consumers/providers,
Settings navigation and browser origin semantics, synthetic public metadata,
network interception and a test-owned Vite server. Protected preparation,
external traffic, real credentials and unexpected writes are forbidden. The
existing integrity harness adds a synthetic default-port HTTPS Avibe control
against actual sandbox CSP/RPC, alongside loopback controls. The catalog's UI
collector resolves Vitest, not Playwright; browser files are supporting pointers,
not catalog-resolved cases. Browser acceptance is run and reported separately.

Focused unit regressions cover the real shared guard and preparation consumers,
EN/ZH rendering, stale status, permissions, direct hook entry, localized
synchronous failures and metadata/standard/cancel behavior. The old capability
helper is private but remains production-used inside `vaultEntryIssue`; loopback,
no-parent-WebAuthn and missing-WebCrypto regressions now use that public boundary.
Copy tests keep every other precondition valid and restore only WebCrypto to
prove it caused the notice, without executing a protected operation.

Desktop-to-browser round trips model an allowed loopback, secure context and
Node WebCrypto, not a mocked assessment. Every resume must reach a permitted
positive state: enabled approval plus binding preparation/warm-up; setup's loss
acknowledgment and enabled Add a passkey; enabled unlock for provision/edit; or
enabled reveal plus context preparation for the same field. Shape-correct inert
contexts and mocked custody hooks allow readiness without real credentials,
network or ceremony. The tests still forbid any automatic protected operation.
Missing browser prerequisites previously let mere presence/no-operation checks
pass while the entire continuation was blocked; those same fixtures now fail
all nine round-trip controls. The four localized copy controls also fail if
restoring WebCrypto leaves an accidental secure-context block in place.

Native frames are `vault/entry-notice/*`; browser acceptance also captures the
real mobile approval dialog. No physical passkey, production runtime, LAN
service, sandbox publication, cryptographic or funds action is part of this
acceptance.
