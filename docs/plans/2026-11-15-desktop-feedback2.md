# Desktop feedback.2 local acceptance

Max approved all sections of `rc19-feedback1-change-list-v1.md` on November 15, 2026 at 12:26:57 +08. Baseline: `b9fe7f196e031a319cbebac4c78072770a6a83af`, installed feedback.1. Work proceeds locally on `feat/desktop-feedback2`; package acceptance precedes PR/push/release/installation authorization.

## Change contract

- Password-mode link: centered dark/themed underlined label with explicit icon spacing; existing credentials and factor handling remain owned by AccountApp.
- Footer: one avatar/name target presents instances when signed in, login otherwise; no account popup or caret. Preserve the actual VersionBadge and remote trusted updater routing.
- Instances: current first, then one native-ordered group, Add instance, Account. Ownership controls computer/group icon, including current shared targets. Offline rows cannot connect or retry; refresh remains a page action. Current has a surface background and restrained green outline/icon/check. Errors remain in the row below its address; Retry replaces the right arrow and shares its vertical center. Existing pending mask, consent disclosure and generation retirement remain.
- Account: email, enabled authoritative local remote-access URL, explicit sign-out on this device and failure retry. Presentation parameters never authorize sign-out. Add guide uses supported Desktop sign-in and target-machine `vibe remote` pairing, followed by return/refresh, without generating keys or deploying anything.
- Real product views drive en/zh light/dark frames. Keep Desktop navigation, widths and density; no iOS sheet mechanics.

## Native boundaries and dependencies

Native owns bridge/hooks/mount and wire fixtures. Authoritative nullable slug, current target metadata, Workbench document preservation and post-login return require its accepted contract. UI does not derive identity from hostnames, keep another MRU store, navigate to a guessed homepage or auto-return merely because an initial state is signed in. New TOTP recovery material must remain visible until acknowledged. Native owns titlebar coordinates and package/TCC evidence. The unrelated draft-attachment lane is excluded.

## Verification

Consumer regression tests first fail on removed popup, current shared grouping and offline click behavior; existing consent, generation and updater checks remain. Cover account explicit action/error retry, shared/offline intersection, changing availability after failure, supported guide return/refresh, long content and password link rendering. Run focused real browser consumers with test-owned storage and denied external endpoints. Run UI lint/test/browser/design types, UI build and the full Desktop build (strict TypeScript + bootstrap + account). Inspect representative scoped product frames; retain logs and exact source provenance in `/tmp/feedback2-ui-evidence`.

No UI fixture is evidence of native authentication, retained Workbench state, installed package operation or TCC access. Lead integrates the fixed source; native rebuilds source-dependent package outputs from the final pin. No PR, push, install or live account/service action is authorized in this lane.

## Independent UI milestone

The owning regression first failed four assertions on the baseline behavior (`consumer-red.log`): popup-based signed-in entry, missing current shared section and clickable offline rows. The updated real-hook/component consumers pass 42 cases (`consumer-final.log`), including explicit account cleanup failure/retry, local-address availability, add-guide return/refresh, offline transition removing Retry, generation fencing, consent and updater behavior.

Real browser fixtures passed 21 final affected account cases (`browser-final.log`): two sizes, English/Chinese, light/dark, grouped current/shared/offline rows, inline error and vertically centered Retry, account and guide navigation, password-link spacing/color, OTP/2FA/recovery, and native sign-out receipt contrasts. The earlier browser run also passed the 8 real AppShell updater/entry cases and 4 stable-press footer cases (`browser-first.log`); its single failed color comparison observed an in-progress theme transition and was corrected to await the final CSS color. Failures remain preserved separately from final screenshots. These are browser/native-peer fixtures, not installed native acceptance.

UI build, full Desktop build, test/browser/design type checks and lint pass (`*-final.log`). The changed canonical design DOM consumers pass 44 captures (`design-dom-final.log`); scoped export renders 52 images without fixture/console errors (`design-export-final.log`). Product frames include `desktop/instances/{email,account,signed-out,instances,current-shared,offline-shared,switch-error,profile,profile-disabled,sign-out-error,add}-{en,zh}`. Inspected the 880px actual inline-error browser capture against the feedback.1 capture, current-shared zh/dark, account zh/dark and initial en/light list. Inspection caught transparent divider tokens and inherited offline text color; both now use the existing visible separator/muted tokens. Exports reside in the Session's existing private Show workspace. No unrelated visual directions changed.

Remaining integration work is explicit: consume native's frozen authoritative slug field in the footer container, implement only the accepted post-login return contract (with recovery-code acknowledgement), and test those consumers after the native-owned types/hooks are supplied. The view accepts an optional nullable `currentSlug`; the current container intentionally provides no guessed value. Normal registration success no longer renders its former completion card, but automatic Workbench restoration is not yet implemented or claimed. Native's existing external-link path supports the guide's HTTPS target in a system browser; the proposed retained-account document must reuse that handler. Lead/native own the lifecycle amendment, final integrated source and package smoke/TCC evidence.
