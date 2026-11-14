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

Focused observable regressions cover footer presentation and switcher/auth transitions; hermetic browser fixtures cover long content, opening/failure/upgrade/offline/consent and both languages/themes. Run UI/account builds, lint, test/browser/design type checks, scoped design shots and inspect results. No live account/OTP, credentials or services. Existing native evidence is not attributed to this candidate.

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
