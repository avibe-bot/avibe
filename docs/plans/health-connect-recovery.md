# Recoverable iOS Health connection setup

The 52.1 field report reached Avibe's consent page, then remained on the
conversation's opening label without completing pairing. The existing Health
settings showed previously requested read types switched off. That does not
prove which native callback stopped; HealthKit hides read authorization.

Keep one connection owner and give setup after consent a finite total deadline,
including authorization, replacement cleanup, pairing, manifest, claim and
failure rollback. Expiry must retire the owner's epoch before releasing the
bridge, preserve pending cleanup, and prevent late work from committing. Reuse
the existing cleanup queue and persistence; add no device protocol or store.
Successful connection still precedes initial upload and means only connection
establishment. Empty Health results remain valid.

Wait for the consent sheet's dismissal before asking HealthKit to present.
Provide neutral settings guidance without claiming to detect denied reads.
Use the existing brand color for a primary consent button, with a secondary
decline action below it, outside the form. Keep the approved button copy.
The consent screen retains its original trusted target card above two
explanation cards: sync destination and retention, then Agent access with
disconnection and deletion. The target card includes the dynamic name, origin,
and read-only data groups. An information button opens the detailed privacy
and connection disclosures separately.

The card distinguishes preparing a connection from waiting for native setup.
A settled native failure permits an explicit retry only while fresh server
metadata still authorizes it. A dispatched native operation continues to own its
slot until native completion; no JavaScript timeout may permit concurrent setup.

Validate immediate (previously determined) and delayed authorization, held
cleanup callbacks, rollback, supersession and late pairing. Exercise real
SwiftUI dismissal and take English/Chinese light/dark screenshots of consent and
card states. Keep all Health data and instance services synthetic. Build unsigned,
use an existing DerivedData serially, and stop builds below 20 GiB free Data
space. No signing, device installation, merge or TestFlight is in scope.

The November 23 implementation began at master
`70593397399c1a7cce389abc1cada42c8c7f0977` and was validated again after
integrating master `56854d06f8b03e1b706f48edb1fcabef3211fa0b` in local merge
`95b4f39e07bb0a596803af7f7a1666321dc09a2c`. The combined unsigned simulator
build passes with Xcode 27.2 and the iOS 27.2 SDK, on the iOS 26.4.1 simulator.
The affected native run passes 244 cases: HealthSync 102,
HealthDataReader 18, and MobileOnboarding 124. These include actual SwiftUI
sheet dismissal, an immediately returned authorization callback, blocked
system cleanup and rollback, expired callbacks running before the timer,
and an old pair response arriving after replacement. Earlier failed runs
remain separate evidence; they were not overwritten.

Web lifecycle coverage passes 122 cases, including explicit retry after a
settled failure and an older shell retaining its pending native owner.
Localization coverage passes 20 cases, and the combined affected Web run passes
165 cases including 23 API-fetch consumers; production, mobile, design, and test
TypeScript builds pass. The first-query help consumer covers a complete empty
window, an older sample, an explicit zero, a recent sample, a missing day,
query error, and locked protected data without inferring a read grant.
The explicit-retry regression fails against master's original web owners
because there is no retry action. Current owners are restored byte-for-byte
after that comparison. AUTH-SETUP-186 passes for both personal and organization
instances using the actual HTTP/SQLite/CLI path and synthetic device data:
abandon before pair, retry with a fresh code, finish the same request, reject
the abandoned code, then read and revoke.

English and Chinese light/dark consent captures use the production view and
the synthetic display name `max`. The two approved explanation cards supersede
the earlier three-paragraph preview; the trusted target card remains above
them. The owner approved the recovery and optional-help wording on November 23
at 15:10. Both notices use neutral wording; the latter provides a manual Settings
path and has no settings button. The public App-settings page inspected on the
simulator had no visible Health entry; the probe's captures and original test
source remain outside the product change. No claim is made that synthetic
callbacks identify the callback that stalled on the physical device.

Both notice messages and their single dismiss action are consumed through
the production alert host in English and Chinese, in both themes. Captures wait
for the sheet and alert presentation to finish; a controller acquiring a window
alone does not mean its presentation is complete. The stricter capture-only
wait is verified separately from the full native suite. No UI retry or relaxed
success/identity assertion is used to hide a failed connection.

Final combined-head evidence is kept separately from earlier attempts:
`/tmp/health-connect-integrated-native-en.xcresult` (244 passed) and
`/tmp/health-connect-integrated-native-zh.xcresult` (2 capture tests passed),
both exit 0 with no skipped tests. The native runs report a SwiftUI
"Publishing changes from within view updates" warning during notice captures;
the alert host's existing binding setters are unchanged. This warning is
recorded rather than counted as a test failure or a demonstrated field cause.
Integrated AUTH-SETUP-186 passes twice, and changed-Python Ruff checks pass.
The final English/Chinese light/dark captures and a SHA-256 manifest are under
`/tmp/health-connect-previews-integrated`; the Chinese light consent image has
zero pixel differences from the approved v4 reference. No physical-device or
signed-build validation was performed for this change.
