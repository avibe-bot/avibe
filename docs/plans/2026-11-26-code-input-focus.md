# Desktop code-input focus

Initial base: `origin/master` fetched on 2026-11-26,
`2ff286ba6812e8a9b9cd2505d58d7bd23c67d870`. Rebased onto freshly fetched
`985182d521ae6b18b5ab39e1d90e0c100593b267` after the sidebar change (#4311) landed;
the OTP implementation and tests are unchanged by this integration.

Max confirmed that clicking the code field again makes typing work. The shared
`OtpCells` painted a caret without focusing its transparent input, so the
Desktop TOTP screen appeared ready for typing while keystrokes had no target.
Email verification uses the same component.

## Change contract

- Desktop code steps focus the real input on entry and when a busy operation
  finishes. A shared page in a non-touch browser follows the same policy.
- The existing sign-in platform and `isTouchCapableDevice` helper own this
  distinction. iOS/touch pages keep their existing tap-to-focus keyboard policy.
- CSS `focus-within` controls the existing active border, ring, and caret.
  Deliberately leaving the input removes them; ordinary value/error rerenders
  do not steal focus back.
- Existing disabled behavior, one-time-code attributes, six-digit normalization,
  automatic submission, error state, and step ownership stay with their current
  owners. No native window, timeout, Keychain, Passkey, copy, or layout change.
- Initial implementation and validation were local only. Max authorized PR and
  gated merge on 2026-11-26 at 11:28, with installed acceptance deferred to the
  next signed Desktop TEST RC alongside #4311. Release is owned by a separate
  lane; installation still requires separate authorization.

## Validation

The focused component tests cover entry, bulk paste, clearing, busy/error
recovery, deliberate focus leaving, and iOS/touch preservation. Existing shared
sign-in and Desktop/mobile suites exercise the consumers and authentication
state machine.

The production-bundle probe reuses the earlier diagnostic fixture with fake
native bridges. Chromium and WebKit cover direct TOTP, password-to-TOTP,
Passkey-step-to-TOTP, restored/requested email code, non-touch browser and
iOS-shaped touch contexts. It checks real focus, six cell click targets, caret
and border visibility, single-edit six-digit insertion, busy opacity, errors,
retry, and clear-on-network-error. Paste is dispatched in unit tests; browser
bulk insertion uses the same input path without touching the system clipboard.
All external requests and credential calls are refused.

Design references: `desktop/sign-in` renders the production flow;
`mobile-app/welcome` and `mobile-app/two-factor` are static design-kit fixtures
that depict a focused code field. Their focus handoff now explicitly ties the
production active decoration to actual focus. Spacing, colors, type, copy, and
dimensions are unchanged.

Reproduction script and results belong to the diagnostic matter:
`~/.avibe/bots/main/matters/2026-11-25-desktop-signin-2fa-passkey/evidence/implementation/`.
The browser fixture does not prove physical iPhone keyboard behavior or native
Mac window activation. Real installed-package acceptance remains separate.
