# Claude auth observation without launching Claude

Date: 2026-10-14. Replaces the unmerged timeout/supervision experiment in PR #2550.

## Change contract

Ordinary Settings/auth and Direct connection reads must not start Claude Code,
refresh OAuth, write credentials, or create a work directory. Read the existing
native OAuth store with `allow_secret=False`; do not read Keychain values or
introduce new stores, background probes, leases, recovery journals, or caches.
The existing Direct snapshot exclusion remains unchanged.

Preserve existing launch credential precedence using `build_claude_subprocess_env`.
A configured API key wins for the active connection. Local OAuth material proves
presence, not remote validity; missing means absent, opaque/denied/invalid means
unknown. Unknown must not overwrite a working API-key state or mean logout.

Claude's explicit OAuth flow already verifies the login and commits it under its
owner. The existing Model Hub adapter consumes only successful flows and stores
its completion metadata on that flow (`source_signed_in`); opaque status cannot
refute the verified result, while a concrete competing API-key selection still
wins. Do not spawn a second status CLI or change AgentAuthService lifecycle. This
is a completion receipt, not a perpetual readiness cache. A Settings confirmation
may acknowledge that successful login while still displaying an uncertain current
Keychain observation and not marking the backend ready.

## Validation contract

- Real test-owned file and fake Keychain metadata at the GET/read boundary;
  forbid subprocess spawn, network, Keychain secret reads and writes. Assert
  presence/absence/uncertainty, raw saved credential signal, token and key
  precedence. The no-spawn regression must fail against the pre-fix CLI probe.
- Existing backend connection cases retain native-store selection and auth
  precedence; exceptions remain unknown. The auth flow scenario covers verified
  completion followed by opaque status, one login verification, and duplicate
  start exclusion until the Source is committed.
- UI test covers successful Claude OAuth followed by opaque Keychain observation:
  no cancellation, no false connected indicator; ordinary unknown stays unknown.
- Run focused Python tests, frontend tests/build and lint before push; GitHub CI
  and exact-head Codex review plus zero unresolved threads gate merge.

## Non-goals / limitations

No live OAuth test or runtime restart. No remote token-validity claim from file
presence. An existing macOS Keychain login may stay unknown in ordinary readiness
without reading its secret or launching Claude; do not invent a logged-in state.
No implicit authorization to merge PR #2547 or deploy either change.
