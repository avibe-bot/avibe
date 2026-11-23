# iOS alarm and reminder coexistence

Baseline: `56854d06f8b03e1b706f48edb1fcabef3211fa0b`, fetched from
`origin/master` on 2026-11-23. This is the Native adapter lane of the approved
device-routed system schedule design.

## Behavior and scope

iOS 26 and later advertises both `alarm` and `reminder`. Alarms use the existing
AlarmKit implementation; reminders use the existing EventKit implementation.
Each framework reports its own authorization, and every new item still requires
the existing Native confirmation. Denying one permission does not select the
other kind. iOS 17.4–25 remains reminder-only.

Change only the LiveSystemSchedulePlatform capability declaration and reminder
dispatch guard, their owning tests, and English documentation. Keep the I3
canonical envelope, installation identity, owner epochs, effect slots, receipts,
recovery, cancellation, logout retention and bridge contract unchanged. Runtime
device targeting is a separate lane; this change does not establish a trusted
target or bypass its claim checks.

The existing confirmation already selects each kind's localized title, action
and disclosure. Both framework usage descriptions are present. No new copy,
permission category, entitlement or signing setting is required by this change.

## Regression contracts

- Live capabilities must expose every platform-supported kind and the EventKit
  authorization reported by EventKit. The existing test currently requires
  reminder support to be absent on iOS 26, so update that owner-boundary test.
- A reminder must reach the existing EventKit authority fence on iOS 26.
  Exercise the real adapter with its existing event-store dependency; a
  test-owned EventKit subclass declines permission requests and prevents any
  calendar lookup or save. An invalidated request must return `staleContext`,
  not the former OS-version `unsupported` result. This checks dispatch without
  granting permission or creating an OS item.
- Extend the existing coordinator idempotency test across both kinds.
  The same operation must replay its original identity and cancellation must
  remove it once, with authorization read for the requested kind.

Run the changed owner-boundary tests against the baseline adapter before
running the fixed adapter. Keep their failures and exits as red evidence.
Run the owning SystemSchedule tests and existing confirmation/bridge consumers
in a coordinated isolated simulator, using fresh test-owned ledger/defaults,
DerivedData and xcresult paths. Lightweight parsing or type checking is not an
XCTest or OS-effect result.

## Acceptance and delivery boundary

Simulator and unsigned builds cannot establish real AlarmKit ringing,
EventKit notification delivery, hardware permission behavior, locked-data
protection or physical power-loss recovery.

The repository's signed `ios-testflight` workflow accepts only `master` and
pins Xcode/SDK 27.2. A read-only check on 2026-11-23 found local development
signing identities but no profiles in either standard provisioning-profile
location (one directory absent, the other successfully enumerated and empty).
No target-device UDID was supplied or discovered. Existing development-profile
coverage of the App, NotificationService, LiveActivity and Max's test device
therefore cannot be established. Neither route is authorized for execution
by this lane.

Before a user-visible Native delivery, the lead coordinates one simulator
window and agrees the physical acceptance route with main. If existing
development provisioning covers the intended device and targets, Max must
authorize the signing/install acceptance step. If the available route is
master-only TestFlight, Max must explicitly approve the pre-acceptance PR
exception before a PR is opened. Do not change Portal state, profiles, bundle
identifiers or entitlements to manufacture a route.

Physical acceptance uses a dedicated test context and unique titles: confirm,
create, observe the matching receipt and exact cancellation for each kind;
check independent permission denial, replay without duplication, and logout
retention. It must not inspect or delete unrelated personal items. Test results
must name the actual binary, Runtime revision and evidence layer.

No push, PR, signature, installation or TestFlight dispatch is authorized here.
Do not start a simulator until the lead allocates it; stop heavy builds below
20 GiB free space and retain existing devices and other owners' data.

## Preparation status

The two production edits and their tests are prepared. Swift syntax parsing
passes for both changed Swift files; the test-only EventKit overrides type
check against the installed Simulator SDK. These are lightweight compiler
checks, not an App build or XCTest execution. Baseline red, fixed green,
confirmation/bridge consumers, unsigned builds and physical acceptance remain
pending the coordinated simulator window and delivery-route decision.
