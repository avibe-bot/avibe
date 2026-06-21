# Fix: PWA login always fails with `invalid_oauth_state`

## Background

Installed (standalone) PWAs on iOS deterministically fail Avibe Cloud remote-access
login: after approving the avibe.bot consent screen, the user lands on
`/auth/callback` and gets `invalid_oauth_state` every time. Normal browsers work.

## Root cause (evidence-backed, from the master regression env)

Topology: the PWA and `/auth/callback` are both on the tunnel host
(`test-app.avibe.bot`); the authorize/token endpoints are on `avibe.bot` (parent
domain, cross-origin but same registrable domain → same-site).

Diagnostic logging on the callback (regression) showed, on every PWA failure:

```
cookie_parsed=True  cookie_state_rid=<A>  url_state_rid=<B>  url_state_valid=True   (A != B)
handshake_cookie_present=True  sec_fetch_site=same-site  ua=...iPhone OS 18_7...
```

So the handshake cookie **is** delivered and valid, and the callback URL's `state`
is **also** a valid token we signed — but they are **two different states**.

Why: iOS standalone PWAs open the cross-origin avibe.bot authorize page in a
separate in-app-browser context, while the PWA's main webview independently
re-mints its own `GET /` → state + handshake cookie. The cookie the callback reads
therefore belongs to a *different* `GET /` generation than the consent the user
actually approved. The existing check `cookie.state == url.state` is the wrong
invariant in this multi-context environment.

(An earlier hypothesis — the cookie was a session cookie dropped across the
excursion — was disproven by `handshake_cookie_present=True`. Adding `Max-Age`
only changed the symptom from "absent" to "present-but-stale".)

## Fix

Stop requiring `cookie.state == url.state`. Recover the PKCE secrets by the
**signature-verified URL state** instead:

- `GET /` (`_redirect_to_vibe_cloud_login`): generate `state` (signed, with random
  id `r`), `nonce`, `code_verifier`. Persist `{nonce, code_verifier, next}`
  **server-side keyed by `r`** (single-use, 5-min TTL), in addition to the existing
  cookie.
- `/auth/callback`: verify the URL `state` signature, then:
  - **cookie-first** — if the cookie is present and its state matches the URL state,
    use the cookie's secrets (unchanged strong per-browser binding for normal
    browsers);
  - **store-fallback** — otherwise look up the server-side handshake by the URL
    state's `r` (the iOS PWA / cookie-desync case);
  - if neither yields a record → existing one-shot retry, then the friendly
    re-login page.

Server-side store: an **in-memory** dict keyed by the state id (`vibe/remote_access.py`),
single-use (`pop` under a lock), TTL-pruned, with a size cap. The UI server is a
single process that handles both the redirect and the callback, so memory is shared
and admission/single-use are trivially atomic under one lock. It is deliberately
*not* on disk — the handshake is short-lived and a mid-flow restart just means the
user logs in again; keeping it in memory removes the disk/inode DoS surface and the
file-cleanup/atomic-rename machinery entirely.

## Security

The change does not weaken the real gate: **which identity may complete OAuth for
an instance is enforced by the avibe.bot backend** (`isEmailAuthorizedForInstance`);
the local instance only trusts backend-issued tokens (audience/issuer/nonce
checked at exchange). The `state` remains HMAC-signed and single-use, so it cannot
be forged or replayed. The cookie's state-equality was a defense-in-depth layer
that is unavailable (and counter-productive) in standalone PWAs.

Codex review (gpt-5.5, xhigh) hardening applied:

- **Store-fallback requires a valid same-origin handshake cookie to be present**
  (its state may differ — that's the PWA desync — but it must parse/verify). This
  proves the browser actually started a login on this instance, so a bare
  `code+state` callback URL can't be replayed in a browser that never did. The PWA
  always carries such a cookie, so the fix still works.
- **`pop_oauth_handshake` is atomic single-use** via `os.replace` to a unique
  private name before reading, so concurrent callbacks for one `rid` can't both
  consume the same record.

### Per-browser binding for the store-fallback (login-CSRF closed)

A bare `code+state` callback URL must not log a *different* browser in. The
store-fallback is therefore bound to a **stable per-browser device cookie**
(`__Host-vibe_oauth_device`):

- The login redirect sets it once (reused on later flows, 180-day TTL). Unlike the
  per-flow handshake *state*, it is not regenerated per `GET /`, so it stays
  consistent across the iOS authorize excursion.
- The handshake record stores `hmac(session_secret, device_id)`. The callback's
  store-fallback only proceeds when the request's device cookie hashes to that
  value — proving it is the same browser that started the flow.
- An attacker cannot present a victim's device cookie (HttpOnly, Secure,
  per-browser), so inducing a victim to hit an attacker's `code+state` callback no
  longer logs the victim in as the attacker.

This was chosen over a `localStorage`-nonce + JS-finalizer flow because it needs no
interstitial pages, no client JS, and no change to the normal-browser login UX —
while giving the same binding. Its one assumption (iOS keeps the persistent device
cookie stable across the excursion) is verified on the regression PWA; if that ever
fails, the `localStorage`-nonce variant is the fallback.

### Hardening the unauthenticated path (Codex review)

The login-start redirect and `/auth/callback` are reachable without a session, so
unauthenticated floods are the root of the resource-growth concerns. Addressed at
the highest layer plus backstops:

- **Root: per-client rate limit** on the unauthenticated `/auth` path (`ui_server.py`,
  fixed window keyed by the Cloudflare-forwarded IP). A flood is `429`'d at the door,
  so the downstream store and diagnostics stay bounded; a real login spends only a
  couple of requests, far under the budget.
- **In-memory store with a size cap** — no disk/inode surface; the cap sheds new
  entries when full (preserving in-flight logins) as a backstop.
- **Rate-limited diagnostics** — every unauthenticated-reachable failure log goes
  through a per-key throttle; the capacity warning is throttled; the success-recovery
  line is `debug`.
- **i18n** — the re-login page copy lives in `vibe/i18n` (`remote_access.oauth_error.*`,
  en + zh) and renders in the browser's `Accept-Language` (the only server-readable
  locale signal pre-auth; the SPA keeps its language only in localStorage).

## Testing

- Unit (`tests/test_ui_remote_access_auth.py`):
  - new: valid-but-mismatched cookie + matching server-side record → callback
    completes via the store, using the record's verifier/nonce (not the stale
    cookie's).
  - new: login redirect persists the handshake cookie (`Max-Age`).
  - existing cookie-path / retry / legacy-state / sanitization cases stay green.
- Manual: iOS standalone PWA login on the master regression env (`test-app.avibe.bot`).

## Rollout

1. Deploy to the master regression env; confirm PWA login succeeds (and the
   `recovered via server-side handshake` info log fires).
2. codex review (auth-path change) → open PR.
