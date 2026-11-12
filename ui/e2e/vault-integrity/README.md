# Offline Vault integrity and entry fixtures (AUTH-SETUP-136 / 137)

`release/` is an exact copy of vault-sandbox's reviewed candidate
`retained/v/2e88ccc707cd0eeeedacf77e87ae80b8e8a14dc769aab6eaf736d7860835e514/`
from sandbox source commit `6edd2e1406008bc9a4665c82d706d630bdc5d464` (#37,
tree-equal reviewed head `7970b211a9c2219f5202977a09e5c58e55563443`).
`source.json` lists source-input hashes; the sandbox commit containing this
snapshot locates its source. It is reproduced twice using the owning repository's
release tools, not fetched from a deployed host. The packaged authority is
`vibe/data/vault_sandbox_release.json`; `hosting.json` copies sandbox `vercel.json`.

`legacy/` copies the frozen runtime HTML, JavaScript and CSS plus manifest from
sandbox `retained/legacy/`, independently reproduced from reviewed commit
`415435548e63ae08942c5b26b0844d5504824d6f` (#25). Sourcemaps are deliberately omitted
because no runtime loads them. This separate test simulates the existing `dev`
parent handshake; the new parent never selects or trusts this compatibility root.

The fixture uses the actual Avibe client and actual built sandbox code. All
non-fixture external network is denied; browser credential APIs are denied.
`public-context.json` is public synthetic signed-operation metadata. Tests open
confirmation details but never approve, sign, decrypt, create passkeys or call a
real Vault. Changing served bytes without changing the local pin exercises
preflight rejection and, after good preflight, native browser SRI rejection.

`entry-fixture.html` mounts the actual Vaults page, API provider and Settings
navigation surfaces. `entry.spec.ts` intercepts synthetic HTTP LAN/custom HTTPS/
non-default-port Avibe addresses and serves only local Vite modules and inert
API replies: the IP/hosts are never contacted. It records and rejects protected
preparation, sandbox loads, unexpected writes and credentials. Deny, metadata
rename and standard signing exercise only synthetic API endpoints. The Settings
CTA must navigate without a document request, restore the approval/dialog, and
never enable Remote Access or put a draft in a URL. Existing modal suspension
clears typed inputs; the form warns before that navigation. These tests claim
only early entry UX, not passkey or secret-operation acceptance.

Run `npm run test:vault-integrity` from `ui/` (Chromium and WebKit, then typecheck).
The suite is part of browser CI. It does not establish live publication, physical
iOS/PWA activation or real-key acceptance; see `docs/plans/vault-sandbox-integrity.md`.
