# Native HealthKit delivery

## Change contract

Implement HealthKit v2 S3 against the orchestrator-owned
`healthkit-v2-one-way-read.md` (2026-11-18), sections 3/5/7/8 and appendices A–E.
The instance lane owns the canonical English `health-data.md` specification.

Only the configured instance main frame may invoke `health.connect`; its reply
contains a connection state only. All other `health.*` methods remain unsupported.
Native consent precedes read-only system authorization and pairing. Empty results
never imply denied authorization. This phone syncs to one personal instance.

Credentials live in a private, non-synchronizing ThisDeviceOnly Keychain item.
Health payloads and monotonic sequence/request bookkeeping use protected,
backup-excluded app storage. Account changes stop queries, cancel transfers,
remove credentials, and delete local sync state. Device authentication uses no
browser cookies. Upload success precedes successful refresh acknowledgements.

## Validation and release boundary

Use synthetic HealthKit samples, injected credential stores and a loopback fake
instance only. Verify request/reply boundaries, durable sequencing, deduplication,
HTTP failure policy, logout races, and metric algorithms with XCTest. Build with
isolated DerivedData under caffeinate. Do not read real health data, contact the
production instance, reset a simulator, change Apple capabilities/profiles, sign
an archive, publish TestFlight, or merge without separate owner authorization.

Physical-device authorization, protected-data behavior, background wake latency,
and signed archive validation remain explicit integration checks after approval.


## Evidence (2026-11-18)

- Unsigned generic iOS Simulator build and full build-for-testing passed with
  isolated DerivedData. No archive or provisioning profile was inspected.
- The first focused simulator run passed all 82 tests. After strengthening the
  background completion ordering, all 34 HealthKit tests passed; an existing
  notification cleanup test's process exited during overlapping simulator lane
  activity. A final isolated selection passed all 37 tests: 17 reader/consent,
  18 device/sync, the health notification landing path, and that cleanup case. The reader suite passed all 18 tests after adding modern
  workout activity mappings.
- The native consent page was rendered in the simulator against a synthetic
  personal destination and its XCTest image attachment was inspected.
- Archive/profile validation: 113 hermetic pytest cases passed, including strict
  boolean entitlements, extension isolation, and read-only purpose strings.
- Changed Python files passed Ruff; bundled mobile UI preparation passed.

Physical-device HealthKit permissions, real background delivery, signed archive
validation, Apple App ID/profile changes, and TestFlight remain unperformed.
