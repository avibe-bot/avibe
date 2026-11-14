# Desktop login layout and account entry repair

## Contract

The signed-out footer opens the existing guarded account presentation directly.
Only a signed-in account opens the menu; its chevron reflects the expanded state.
Instance/sign-out actions use existing visible button variants, while local remote
access remains a noninteractive status. The footer account trigger keeps standard
hover, active-color, focus, and keyboard behavior without press scaling.

The bundled email/verification card sizes to its content and centers vertically
in the available content area. At short window heights, the content scrolls while
the header remains accessible. Existing login, factor, consent, cancel, and
instance-selection commands do not change.

## Diagnosis and ownership

Base: master d8187b453f7f272db96ee7ecfe14c7e940c2c4a4 (contains e1285c16).
The installed rc16 Runtime at 3.2.6rc16/fb12bffc2009d094 was inspected read-only.
Its UI index asset SHA256 is
dabab09d2918a518d3352c423147a2b50c3e4bb140d10dc98e7436d57ed2edf7.
It contains the unconditional Popover trigger and fixed rotated chevron found
in source. The bundled account source uses `min-h-full`, stretching its card.
The shared Button `press` utility applies control scaling to the footer trigger.

PR #3688 independently restores the update popup. This branch touches the account
portion of the same footer file; the overlap was reported to main before editing.
It does not import that PR's updater changes or alter its behavior.

Native session seszkwvjjwaz5 owns the reported return-to-Workbench crash and actual
instance-switch completion. UI call sites were handed off: AccountApp sends cancel,
return_local, and select_instance through useAccountClient and desktop_client_request.
Mock browser replies prove UI dispatch, not native window safety or authentication.

## Verification

Existing AUTH-SETUP-177/178 tests cover the account flow. Extend their consumers to
observe direct presentation, action/status distinction, actual card geometry,
chevron rotation, and pressed scale. The old updater-only footer test never checked
signed-out navigation, and the existing browser fixture only checked visible login
fields at a fixed height, allowing a stretched card to pass.

Use isolated HOME/XDG and browser profiles, synthetic bridge state, and blocked
external traffic. Do not send real OTP or interact with the running app. Keep red
and green evidence in /tmp/rc16-login-ui-sesuzge7suz9p. Build both UI and bundled
account assets before push. Delivery stops at exact-head review/CI Merge-ready;
merge, package installation, release, and service operations are not authorized.

Observed red cases: signed-out footer made zero account presentation calls; the
old card grew 200px when the viewport grew 200px; the expanded chevron remained
at 180 degrees; the pressed trigger scaled to 0.94. Repairs pass ten focused
component/protocol tests and 35 browser cases covering both Setup exits, login,
factors, consent, instance dispatch, and the eight new layout/menu cases.
English/light and Chinese/dark screenshots were inspected at both target sizes.

## Shared login design source

The email/code/code-error frames render the same AccountLoginView as AccountApp
using typed synthetic props. The old kit Login markup is removed. The frames
show the card in an isolated stage, without redrawing page chrome or enabling a
native bridge. AccountApp retains the request, busy/error, cancellation, and
native lifecycle ownership. The top-level window guard is unchanged.

After extraction, all ten product tests and 35 browser cases still pass. Design
typecheck passes; scoped capture and export each cover twelve shots (three
states, two languages, two themes), with no console errors or unfixtured API
requests. Email, verification and invalid-code captures were visually inspected.
Private review page: https://max-app.avibe.bot/show/sesuzge7suz9p/ .


## Canonical footer consumer repair

After #3688 merged, the branch was mechanically rebased onto
cbd4af9552eb984780e57870a01a04d91616cd67. The original local VersionBadge and
remote guarded updater presentation remain unchanged. The subsequent review
identified the same design-consumer root cause on the account footer. The
orchestrator independently diagnosed the repeated class and authorized only the
canonical signed-out/account footer states in this PR.

DesktopAccountRow retains projection and presentation hooks, controlled menu
state, native update events, navigation guards, and the real VersionBadge. Its
DesktopAccountRowView receives typed nonsecret projection, controlled menu state,
presentation callbacks, and the local updater node. The four canonical footer
frames use this same view through the real AppShell footer slot and
AppShellFooter, with synthetic props and the product VersionBadge. They do not
install native bridge globals or change the top-level window guard.

The kit no longer routes these canonical states through its Workbench footer.
AccountEntry/Account remain only for proposal consumers that still need them;
instance pages, top/compact layouts, Mini and later phases are not migrated.

The existing design DOM check now requires these exact frame IDs to render the
product row inside the real sidebar footer and rejects the old bottom kit entry.
It failed before the repair on account-en because no product row existed. The
remaining proposal invariants stay in that check; the already-shipped login
card frames correctly have no footer. Existing product tests exercise the
container-to-view wiring and original updater behavior without new bridge mocks.
Repair evidence is under /tmp/rc16-login-ui-sesuzge7suz9p/footer-repair.

After extraction, 63 footer/updater unit cases and 35 actual AppShell/account
browser cases pass. UI/account builds, lint, test/browser types and design types
pass. The updated DOM check passes all 112 frame/theme combinations. Scoped
capture/export produces eight footer images with zero console errors or
unfixtured reads. Signed-out and controlled-open screenshots were inspected;
the open product browser menu and design menu share geometry at 1200x800.
VersionBadge, the bridge and presentation hooks remain byte-identical to the
pre-extraction candidate. Earlier native replay retains its original head pin.
