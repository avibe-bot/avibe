# Restore the master sheet fixture build

## Contract

Restore `AvibeSheetFixture` compilation after #4397 without changing production
Health behavior, entitlement configuration, or target membership. The fixture
compiles the production `NativeBridge` with minimal support types and uses its
default failed Health handler. Add the missing `HealthConnectionOptions` shape
to that existing fixture support file, preserving the four legacy defaults.

## Target inventory

Inspection of every Swift file changed by #4397 and all seven native targets:

| Changed source | Compiling targets |
| --- | --- |
| AppModel, HealthConsentView, HealthDataReader, HealthDeviceStore, HealthSyncCoordinator, WebViewContainer, WebViewController | Avibe |
| NativeBridge | Avibe, AvibeSheetFixture |
| HealthDataReaderTests, HealthSyncTests | AvibeTests |

The added declarations are `HeartStatistics`, `HealthPendingConsent`,
`HealthConnectionOptions`, and `HealthConnectAction`. Only the last two cross
the fixture boundary. The action alias is already compiled from NativeBridge;
the options shape was missing. NotificationService, IrohLib, AvibeLiveActivity
and the fixture test target do not reference the added Health types. No other
target membership change is needed.

## Validation

- Reproduce missing `HealthConnectionOptions` with the unchanged master's
  `AvibeSheetFixture build-for-testing`; the escaping errors are consequential.
- Repeat that build with the fixture-only shape present.
- Build the Avibe scheme for testing, including its tests and extensions.
- Use an owned disposable simulator and unsigned local builds; read no real
  Health data.

## Why the PR check missed this

PR #4397's final run46083603617 used `fast-native`. The workflow skipped its
explicit sheet fixture build, and the native selector builds that scheme only
when files under `ios/AvibeSheetFixtureTests` change. #4397 changed App sources
and App tests, so only the App scheme was built. Master run46085903970 used the
full path and failed in the fixture build before XCTest ran. This is a
deterministic compilation gap, not a simulator test flake. CI policy changes
are outside this repair.

Local result (November 28, 2026): Xcode27.2 / iOS SDK27.2, disposable
iOS26.4 simulator. The unmodified fixture build exited65 with the missing type;
the repaired fixture and App schemes both completed `build-for-testing`. An
initial App build caught a missing generated license notice in local staging;
running the existing license-generation step resolved that preparation issue.
No product change was needed. Both test bundles compiled; XCTest execution
was not requested or performed. The disposable simulator was deleted.
