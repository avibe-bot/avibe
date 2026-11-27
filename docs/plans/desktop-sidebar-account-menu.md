# Desktop sidebar account menu

## Change contract

Base: origin/master `2ff286ba6812e8a9b9cd2505d58d7bd23c67d870`, explicitly fetched on 2026-11-26.
Approved visual source: `chrome/sidebar-account-menu`, design revision `0ad8a11a2a`.

Signed-in Desktop users open the account menu by clicking the sidebar instance name.
The menu shows the signed-in email and the current instance with live connection status.
Switch instance opens the existing native Instances page; Sign out on this device enters
its existing native sign-out flow without an additional confirmation. Offline and switching
connections retain both actions. Signed-out users still open sign-in directly.

The shared navigation guard and native presentation contract own both actions. Settings,
version controls, native account/instance pages, and iOS remain outside this change.
The menu uses the existing first-load translations and product primitives. Information
rows are not interactive; keyboard users can open, move between actions, and dismiss back
to the trigger. Long instance names truncate while the status stays visible.

## Evidence

Unit tests exercise rendering, native presentation intents, keyboard and dismissal behavior,
and state updates. Browser checks use the production build with a hermetic Desktop shell
projection, both themes and languages, and long names. Design captures compare the shipped
component against the approved second revision. Installed native account presentation and
actual sign-out remain integration acceptance; this change neither packages nor installs Desktop.

## Spacing alignment (2026-11-26)

Follow-up base: `7d2ea38f7a860d6b39c84f0f6fdd96d12d512967`. The menu follows
Version & Update's zero outer padding and full-width section
divider, 16px content inset, 12px section/header rhythm and 20px line height.
The existing 8px menu corner radius is preserved. Action text retains the same 16px content inset via 8px group and 8px button
padding. Each action has 6px vertical padding, giving the update rows' 32px pitch
(20px line plus 12px separation); the group's 10px vertical padding puts the
first/last text 16px from the section edge. Hover backgrounds stay 8px inset.

The primary regression is browser geometry: the old 6px-inset divider must fail
the full-width assertion. Existing interaction/unit coverage protects behavior;
no new unit test duplicates CSS layout. Production-build captures compare before,
after and the unchanged Version & Update panel in both languages and themes.
