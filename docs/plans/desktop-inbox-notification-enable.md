# Enable Desktop notifications from the Inbox popover

The sidebar Inbox hover popover has a notification view headed “Back to Inbox”.
Its shared `NotificationMenu` now exposes the existing Desktop notification
preference above “More notification settings”.

On the current local Desktop Workbench, “Enable notifications” calls the
existing `desktop_notifications_set_enabled(true)` and reads the saved preference
back before displaying “Notifications on”. Native preference events and returning
to the foreground refresh the displayed state. A failed write displays an error.
Known system denial displays the existing notification warning.

The account notification switches, Web/iOS push controls, Settings page and shell
remain unchanged. This increment adds no test notification, permission request,
disable action, native command, capability or forwarding mechanism.

## Coverage and boundary

The same menu is used by the sidebar Inbox popover and the full Inbox page. Both
can enable notifications in the permitted local main Workbench. The shell remains
the preference owner and updates its native menu through the existing save path.

Remote Workbenches cannot use the baseline notification capability: its ACL
allows only loopback URLs, and its command guard also requires the current local
Workbench. They display the existing menu-bar guidance. The existing tray checkbox
is a native notification control, but there is no existing page action that
transfers a click from this popover to that checkbox. Remote in-page enablement
therefore remains a separate authorization change requiring independent review.

## Validation and acceptance

Focused frontend tests exercise the actual `InboxHoverPopover` notification view:
successful native confirmation without navigation, failed writes, state changes,
denial and rejected probes. Existing menu, Settings and shell bridge tests are
included. The UI build and lint pass.

A separate same-source native fixture exercises the built UI in a hidden
WKWebView with ephemeral preferences and synthetic local/remote instances. It
checks saved state, native toggle synchronization and rejection boundaries.
This is not installed-package acceptance or an operating-system delivery test.

For installed-package acceptance, first use the approved acceptance-package
lineage and preserve its current host capabilities. After separately authorized
backup and installation, disable notifications in the native tray, hover the
sidebar Inbox icon, open its notification view and click “Enable notifications”.
Verify the badge and tray checkbox agree and that the page does not navigate.
Toggle the tray off again and verify the popover follows it. Restore the original
preference. A remote instance remains outside this increment's functional coverage.
