# Native provider sign-in exchange: one contract for iPhone and Desktop

> **Superseded, kept for history (2026-11-18 17:30).** No server implements this protocol, and
> none will be built for #3946. The backend already turns Supabase tokens into an Avibe session
> (`POST /auth/callback`, `POST /api/desktop/session`); provider sign-in follows that route. See
> `2026-11-18-shared-sign-in-providers.md`. Do not implement from this file.

Status: client-side draft for the backend owner to check, 2026-11-18 (#3946). Not a server commitment. The iPhone half is what
#3945 (`a9885d69`) already sends and parses; the Desktop half is the smallest
addition to it. This is one protocol, not a second one.

## Evidence that the backend half is not in the searched refs

| Searched | Result |
| --- | --- |
| avibe-backend `origin/main` `884a13c` tree and `git grep` for `mobile/auth`, `Avibe-Account-Created`, `exchange` | No `app/api/mobile/auth/**` or `app/mobile/auth/**`; `exchange` only in `/auth/callback` and `/oauth/token` |
| avibe-backend remote branches (`git ls-remote`) | No mobile, provider, Apple or exchange branch |
| avibe-backend PRs: all open (none), and all-state searches for mobile auth, provider, passkey, Apple, desktop sign-in | No exchange PR (#370 is web Apple behind a flag, #317 mobile two-factor, #308 mobile session) |
| `gh search prs --owner avibe-bot "mobile exchange"`, repository forks | Only avibe#3945; no forks |
| Local avibe-backend worktrees | None related |
| Anonymous `OPTIONS https://avibe.bot/api/mobile/auth/start` | 404 |
| #3945 `MobileAuthTransport.swift` | Cites `_tmp/ios-login/contract-backend.md`; not in any repository, not under `~/workspace` or `~/.avibe` |

This says the work is not in these refs. It does not show that nobody has it
unpushed.

## Wire (iPhone today; Desktop adds `client`)

All endpoints are on the control plane (`https://avibe.bot`).
- Requests are same-origin JSON with an `Origin` that equals the control plane.
  iPhone sends them from its hidden WebView; Desktop's native client sends the
  header itself, as it already does for `/api/desktop/session`.
- No `Authorization` header, no redirects.
- `start` starts from a signed-out store.

1. `POST /api/mobile/auth/start`
   - Body: `{provider: google|github|apple, code_challenge, state, client?: ios|desktop}`.
     - `code_challenge` is S256 of a 32-byte verifier.
     - `state` is 32 random bytes as base64url.
     - An absent `client` means `ios`, so #3945 stays valid.
   - 200 for Google/GitHub, or for any provider when `client=desktop`: `{transaction_id, browser_url}`.
     - `browser_url` is exactly `<cp>/api/mobile/auth/browser?launch=<token>`.
   - 200 for Apple with `client=ios`: `{transaction_id, nonce}`.
   - Tokens are canonical 43-character base64url (32 bytes).
   - Errors: 404 `{error:"provider_unavailable"}`; 429 with `Retry-After`.
2. `GET /api/mobile/auth/browser?launch=<token>`
   - Single use, short TTL. Starts the provider's web authorization.
   - For a Desktop transaction, Apple runs here too, as web Sign in with Apple.
3. Return.
   - `ios`: `https://<cp>/mobile/auth/complete?state=…&(code=…|error=…)`.
   - `desktop`: the same query on `avibe://auth/complete`. The completion page also shows an "Open Avibe" link, because a browser may hold a custom-scheme navigation behind a prompt.
   - Query grammar, both clients:
     - Only the keys `state`, `code` and `error`, each non-empty and ≤ 4 KiB.
     - Exactly one of `code` or `error`.
     - `error` is one of `cancelled`, `provider_email_unverified`, `provider_unavailable`.
4. `POST /api/mobile/auth/exchange {transaction_id, code, code_verifier}`
   - The code is single use and bound to the transaction and its client.
   - 204 sets the session cookies (`avibe_session`, `sb-*`), plus `Avibe-Account-Created: 1` when this exchange created the account.
   - Errors: 400 `{error:"invalid_or_expired"}`, 403 `{error:"provider_email_unverified"}`, 429 with `Retry-After`.
5. `POST /api/mobile/auth/apple {transaction_id, identity_token, authorization_code, code_verifier}`
   - Native Apple, `ios` only.
   - Apple receives the nonce as lowercase hex SHA-256 of the server nonce.
   - Same answers as `/exchange`.
6. Then the existing `GET /api/mobile/session` decides the result: signed in, the 403 two-step gate, or 401.

## Client duties (unchanged from #3945, mirrored on Desktop)

- The verifier and state stay in native memory.
- The client opens only a `browser_url` of the exact shape above.
- It compares state in constant time and accepts only the exact callback grammar.
- Any account transition (cancel, sign-out, another sign-in) closes the window and drops a late result.
- A failed attempt leaves a fresh signed-out store.
- Desktop: macOS uses `ASWebAuthenticationSession` with callback scheme `avibe`; Windows uses the default browser and the existing `avibe://` single-instance forwarding.
- PKCE only stops a captured code from being redeemed without the native verifier (RFC 8252 §8.1). It does not by itself establish that the whole return chain is safe; state checking, transaction binding, single-use consumption and subject binding need their own review.

## Owner questions (not decided by #3946)

1. Who owns the backend half, and does a teammate already have it unpushed?
2. Which account an exchange reaches when an email account, an existing Google or GitHub identity, or an Apple Hide My Email address is involved. This is the backend's existing rule, documented, not changed.
3. Apple audiences:
   - `ios`: the iPhone bundle ID.
   - `desktop`: the web Services ID.
   - Is Sign in with Apple enabled for the team, and when?
4. `lib/auth/passkey-session-adapter.ts` refuses passkey sign-in for accounts with an `apple` identity. Is this intended once Apple ships?
