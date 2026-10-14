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
Read `CLAUDE_CODE_OAUTH_TOKEN` from the applicable user/project/local settings as
well as launch env; a settings value overrides inherited env, including an
empty-string mask. These reads are separate from the Anthropic mutation/backup
key list so project credentials and Claude-owned tokens remain untouched by
existing auth saves.

Claude's explicit OAuth flow already verifies the login and commits it under its
owner. The existing Model Hub adapter consumes only successful flows and stores
its completion metadata on that flow (`source_signed_in`); opaque status cannot
refute the verified result, while a concrete competing API-key selection still
wins. Do not spawn a second status CLI or change AgentAuthService lifecycle. This
is a completion receipt, not a perpetual readiness cache. A Settings confirmation
may acknowledge that successful login while still displaying an uncertain current
Keychain observation and not marking the backend ready. If controller application
is pending/unavailable, keep that receipt only on the existing pending confirmation
for Refresh. Settlement, a new login/removal, method changes, or cancellation
invalidate it; ordinary readiness never receives the receipt. The form owns
readback/application errors rather than cancelling an already completed login.
A pending receipt must still own the confirmation when its asynchronous read
finishes; a new OAuth action or auth-method selection invalidates both that
owner and the read generation before a late read can mutate presentation.

Native cleanup capability is separate from OAuth certainty. Opaque native
containers may offer explicit cleanup without claiming sign-in. Environment- or
settings-supplied tokens are not removable by native logout: the UI explains
this, and the Web removal entrypoints refuse before constructing the mutation
service rather than reporting a false sign-out.

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
  AUTH-SETUP-910 also covers Settings/onboarding application recovery via Refresh
  and a new failed attempt superseding the old pending receipt.
- Run focused Python tests, frontend tests/build and lint before push; GitHub CI
  and exact-head Codex review plus zero unresolved threads gate merge.

## Credential observation boundary (review circuit-breaker decision)

The first reviewed head missed receipt retry ownership; the next two exposed the
same producer-side class: reconstructing active auth from only part of Claude's
settings inputs. Stop adding per-variable exceptions. The owning decision is one
stateless, read-only settings observation in `vibe/claude_config.py`, separate
from the existing user-settings mutation/backup helpers:

- Observe the configured default working directory (backend override, then
  runtime default); do not create it or infer settings for every Session.
- Merge user, project and local auth inputs in native precedence, retaining
  explicit empty-string masks. Account for the documented repository-root /
  main-checkout local file and its legacy cwd fallback using filesystem metadata
  only. Never run git or Claude for this read.
- Use the merged inputs only for effective auth selection. Editable saved-key
  metadata remains the user-settings/V2 migration projection; project credentials
  must not be copied into user settings or surfaced as a removable saved key.
- Settings and Direct connection observation consume this resolver. Model Hub
  completion uses the same global readback only after its owning login succeeds
  and settings-source exclusion succeeds; a reader exception or invalid response
  is not success. Per-turn routing admission is outside this change.
- A concrete competing key vetoes subscription completion. Unreadable, malformed
  or unsupported settings are a failed observation, not the opaque-native-store
  uncertainty that a verified OAuth receipt may override. Do not execute helpers
  or emulate remote/managed policy discovery to manufacture a positive result.
- Cover the source/precedence class with real temporary files through Settings,
  Direct and successful-flow consumers, including masking, nested roots/worktrees,
  parse failure, and no subprocess / no filesystem mutation guards.

Evidence: `CLAUDE_SETTING_SOURCES` and the actual launch `cwd` in
`core/handlers/session_handler.py`; Claude's public [settings precedence and local
file locations](https://code.claude.com/docs/en/settings#where-claude-code-keeps-the-local-file-in-a-git-repository)
and [environment precedence](https://code.claude.com/docs/en/env-vars#precedence),
read on 2026-10-14. No process lifecycle changes or persistent cache are authorized
by this correction.

## Fourth-round scope correction

Review of `e1cc5f281` exposed scope creep in adding this default-cwd observer to
per-turn native-Source readiness. That extension is reverted: it did not carry
the actual turn cwd and added filesystem work on the controller loop. Neither a
new per-turn admission contract nor controller routing changes belong in this
status-read fix; the baseline routing behavior is preserved.

The Windows and unowned-repository findings contradict the public local-file
location rules cited above. Those cases deliberately keep the local file beside
the cwd's shared file. Keep that behavior and its explicit fixtures, rather than
reading an inapplicable root file or rejecting supported shared checkouts.

The actionable consumer gaps are handled at their existing owners: native
cleanup versus external-token capability, removal refusal for external tokens,
and receipt/read invalidation when a new action races a pending Refresh. No
credential writer or AgentAuthService lifecycle is redesigned.

## Non-goals / limitations

No live OAuth test or runtime restart. No remote token-validity claim from file
presence. An existing macOS Keychain login may stay unknown in ordinary readiness
without reading its secret or launching Claude; do not invent a logged-in state.
No implicit authorization to merge PR #2547 or deploy either change.
