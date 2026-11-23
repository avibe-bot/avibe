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
- Exercise each kind through the existing WK bridge and real AppModel:
  no platform create before confirmation, the selected kind in confirmation
  and receipts, and one exact cancellation despite a retry. Only OS effects
  use the existing platform stand-in; this does not certify real OS effects.

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

On 2026-11-23 at 16:02 +08, Max approved the exception allowing a PR before
physical acceptance, followed by TestFlight after a separately authorized merge
and dispatch. The lead relayed that approval in run `2c1c4e71a2a6`. After the
coordinated simulator consumer checks, this lane may open an English PR and
follow review and CI to Merge-ready. Merge, signing, installation and
TestFlight dispatch remain separately authorized actions. Do not change
Portal state, profiles, bundle identifiers or entitlements to manufacture
another route.

Physical acceptance uses a dedicated test context and unique titles: confirm,
create, observe the matching receipt and exact cancellation for each kind;
check independent permission denial, replay without duplication, and logout
retention. It must not inspect or delete unrelated personal items. Test results
must name the actual binary, Runtime revision and evidence layer.

Push and PR creation wait for the coordinated simulator consumer checks.
Do not start a simulator until the lead allocates it; stop heavy builds below
20 GiB free space and retain existing devices and other owners' data.

## Simulator evidence

On 2026-11-23, both owning regressions failed against the unchanged baseline
adapter for the intended capability and dispatch reasons. The fixed adapter
passed 57 isolated tests on iOS 26.4: all 50 SystemSchedule tests, five
WK/AppModel consumers (including both kinds through confirmation and
cancellation), and two bridge/identity consumers. The OS stand-ins did not
create real alarms or reminders. This is Native consumer evidence, not
physical-device acceptance.

Commands, source hashes, exits and xcresults remain in the task's matter
handoff. Physical acceptance remains pending the separately authorized merge
and TestFlight route.
