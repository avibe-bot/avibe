# Offline Vault integrity fixture (AUTH-SETUP-136)

`release/` is an exact copy of vault-sandbox's reviewed candidate
`retained/v/eef0b4fe30cfb1cf35507b4aae5144dabfc0bfff66b49ef1d26b5016a52546c8/`.
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

Run `npm run test:vault-integrity` from `ui/` (Chromium and WebKit, then typecheck).
The suite is part of browser CI. It does not establish live publication, physical
iOS/PWA activation or real-key acceptance; see `docs/plans/vault-sandbox-integrity.md`.
