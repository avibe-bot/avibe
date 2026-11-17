# Desktop rc19 local acceptance

Approved input: `rc19-final-change-list.md` v2, Max 2026-11-14 21:00 (password login added 21:00:58). Base: `57a3630976a4970dbd36b10ef392e99029ff39a8`. Delivery is a local acceptance branch before any PR, push, release, or installation.

## Behavior and ownership

- Items 1–6: padded footer trigger, hostname/remote name and icon, animated chevron, grouped account menu with authoritative enabled local remote URL, direct sign-out through native authority.
- Items 7–11: retain grouped switcher during opening, mask other rows, attach failure/retry to selected row, consent modal with complete scope disclosure, authoritative local identity deduplication, offline instructions and refresh, wrapping names/email.
- Items 12–15: shared centered email/OTP/password forms, authenticator/recovery, Workbench locale propagation, consistent English/Chinese copy.
- Product views are the source for changed design frames. Containers own native actions and lifecycle. Never weaken top-window, generation, direct-result, or escape guards.
- Native owns agreed bridge additions, fixtures, local identity/URL/locale authority, password action and sign-out presentation. UI does not guess identity or call backend authentication from a browser.
- Updater, Setup, remote access toggle, Mini/notifications, iOS and later phases keep existing behavior.

## Verification

Focused observable regressions cover footer presentation and switcher/auth transitions; hermetic browser fixtures cover long content, opening/failure/upgrade/offline/consent and both languages/themes. Run `npm run build` in both `ui/` and `desktop/`: the full Desktop build checks its stricter imported TypeScript graph and produces bootstrap plus account assets. Also run lint, test/browser/design type checks, scoped design shots and inspect results. No live account/OTP, credentials or services. Existing native evidence is not attributed to this candidate.

## Progress

The assigned branch began clean. Lead integrated native's immutable A4 TypeScript commit as `92aa3fcd3b6125877aef4cbd8b60d3c81cbcbf64`; UI changes do not edit those reserved files. The account container consumes authoritative identity/URL, password actions, admitted locale and direct sign-out receipts.

Shared product views now render the login, second factor, switcher, consent and footer frames. Selection failures attach to the accepted reply generation, including a native broadcast that advances generation before its reply. A newer signed-out generation suppresses stale errors. Escape remains effective during slow consent approval. Automatic logout requires the current get-state receipt, is deduplicated across same-document remounts, and defers dispatch until its effect is still mounted. Bare views and late/non-get-state results cannot trigger cleanup; a failure offers an explicit user retry.

## Local evidence (November 14, 2026)

Evidence root: `/tmp/rc19-ui-evidence`. All fixtures, credentials, storage and browser contexts are synthetic/test-owned. No real account, OTP, Keychain, production service or installed app was used.

- Red evidence: `footer-red.log` rejects the old remote-access status row; `switcher-red.log` rejects the old whole-page switching/consent and missing row failure behavior. Original browser failures remain in `browser-first-failures/`; they exposed Escape during pending approval, which is now covered explicitly.
- Focused final unit suites: 49 passed (`unit-final.log`), including exact protocol consumers, arbitrary generation advancement/stale contrast, unknown consent scopes, TOTP/recovery material, authoritative local ID versus unknown, updater scope, sign-out receipt remount/late-result fencing and delayed Workbench locale capture.
- Browser suites: 43 passed (`browser-final.log`), covering 1200x800 and 880x600, en/zh, light/dark, real AppShell/footer, login layout, both Setup exits, password/2FA/recovery, inline switching errors/offline/consent, transient secret clearing and locale before the first heading. After the mounted-effect dispatch adjustment, all 5 affected sign-out/locale browser cases passed again (`intent-browser-final.log`).
- UI build, bundled account build, UI lint, test type checks, browser type checks and design types passed. Logs: `ui-build-final.log`, `account-build-final.log`, `lint-repaired.log`, `types-final.log`, `browser-types-final.log`, `design-types-final.log`. The earlier lint failure is retained in `lint-final.log`; no baseline or suppression was added.
- Design export captured 88 scoped images successfully (`design-export-final.log`), and the updated desktop design DOM check passed 156 captures (`design-dom.log`). Impeccable detect reported no findings. Inspected password zh/light, consent en/dark, long-content en/dark, account-long zh/dark, plus email, switching and selected-row-error captures. Footer/update geometry and proposal invariants retain their dedicated checks.
- Browser provenance: locked Playwright 1.64.1 with existing read-only Chromium 159.0.8154.6 via an evidence-only launch preload (`browser-provenance.txt`). The expected Chromium download was stopped after negligible progress; no fake browser symlink or product dependency change.

## Shared design consumers

Frame prefix: `desktop/instances/`, each with `-en` and `-zh`, both themes. Product-backed states: `signed-out`, `account`, `account-remote-disabled`, `account-long`, `email`, `code`, `code-error`, `password`, `password-help`, `two-factor`, `recovery`, `instances`, `empty`, `loading`, `list-error`, `offline`, `switching`, `consent`, `switch-error`, `upgrade-error`, `offline-help`, `long-content`.

Private Show Page: `https://max-app.avibe.bot/show/sesuzge7suz9p/`. Source views are exported there, with synthetic typed props and no privileged native calls. Kit copies of the shipped switcher/connection views were deleted; unrelated placement and later-phase proposals remain.

## Remaining acceptance boundaries

The native lane owns actual provider/session/cookie/2FA transport, local descriptor freshness, locale admission and one-time logout admission/cleanup. Browser peer fixtures prove UI consumption, not those producers. Lead integrates the native implementation into this one acceptance branch; native then assembles the final package. Package smoke, Max's installed-package acceptance, signed-platform checks and any subsequent PR/review/CI remain separate and have not been claimed here. No PR, push, release, installation or new Watch was created.

## Selected-row IPC failure follow-up

Lead's independent review found that rejected or malformed native IPC returned the same `null` as a retired reply, leaving the switcher without a row error. The owning AccountApp + real-hook regression first failed four table groups (`ipc-row-red.log`): current rejection/malformed reply lacked a row alert, and a delayed failure polluted a newer signed-out page.

With native's explicit reserved-file coordination, `useAccountClient.ts` now returns a hook-local `client_failure` outcome for current transport/protocol failures. It is not a wire result and contains no fabricated native state or credentials. `null` remains retirement. Existing action sequence and mounted checks fence cancellation; a selected operation may follow native generation advancement only when the same account's native `switching.pending.instance_id` still matches. No arithmetic prediction of the generation is used. AccountApp attaches the error to that generation and releases only the matching row's pending mask, allowing explicit Retry without changing native projection. Native get-state cleanup errors still display normally without creating a logout receipt.

The follow-up passed 54 focused unit tests (`ipc-unit-final.log`), including eight table scenarios for reject/malformed, unchanged/advanced generation and Escape/newer signed-out contrasts, plus the native get-state cleanup-error consumer. Twelve affected browser cases passed (`ipc-browser-final.log`), including actual rejected and malformed invoke promises after a pending-generation broadcast, row-local errors and successful explicit Retry. Two additional en/zh native-error/offline browser cases passed (`ipc-native-error-browser.log`). The new screenshots are in `ipc-browser-artifacts/` and were inspected. UI/account builds, test/browser types and lint passed (`ipc-ui-build.log`, `ipc-account-build.log`, `ipc-types.log`, `ipc-browser-types.log`, `ipc-lint.log`). Unchanged design views retain the earlier 88 exported shots and 156-capture DOM evidence.

The `aba058c95ffb1c38303e9a809063a30af20c2ef0` archives remain historical. New source-locked archives supersede them; the external evidence manifest records the exact follow-up commit and SHA256 values. Delivery remains local before installed-package acceptance and PR creation.

## Full Desktop build prerequisite

The integrated `648acf5a696a2cec46fde9bb7e42e9b03a595742` candidate exposed 11 indexed-access diagnostics in shared motion/sheet-drag helpers through the account page's Dialog dependency. Desktop enables `noUncheckedIndexedAccess`; the earlier UI build did not exercise that stricter configuration. The retained reproduction is `desktop-strict-build-red.log`.

The bounded correction expresses existing four-capture regex and nonempty-array/touch invariants in six erased type expressions. Runtime guards, animation, gesture behavior, APIs and compiler settings are unchanged. A before/after JavaScript transform comparison is identical after removing the added explanatory comments (`desktop-strict-runtime-equivalence.log`).

Full Desktop build (strict typecheck, bootstrap and account), UI build, test types and lint passed (`desktop-strict-build-green.log`, `desktop-strict-ui-build.log`, `desktop-strict-types.log`, `desktop-strict-lint.log`). Existing motion/sheet-drag and actual AccountApp consumers passed 37 tests (`desktop-strict-unit-green.log`). Eight bundled login-to-consent browser cases passed across both sizes, languages and themes (`desktop-strict-browser.log`). No new tests or visual source changes were needed. Source-dependent package outputs must be rebuilt from the new committed pin; earlier artifacts remain historical.
