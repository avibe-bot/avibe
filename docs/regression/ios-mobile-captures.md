# Real bundled iOS page captures

The `ios-mobile-captures` workflow is **on demand only**. It captures the real
built `ui` mobile page using `MobileOnboardingViewController`, not the static
`FixtureMobile/mobile.html` gesture document. It is not a release, archive,
signing, device, or TestFlight workflow.

## Source and output contract

Dispatch only after the native shell, bundled page, and capture infrastructure
are merged. Select the workflow on `master` and provide `source_sha`, the full
40-character lowercase merged commit to capture. The job checks out that exact
commit, verifies it is an ancestor of `origin/master`, builds `ui` with the
lockfile, and compares the copied fixture `MobileApp` resource directory with
the build output before running. The fixture target—not the production app—has
that additional resource reference.

Example (the operator supplies the verified merged SHA):

```bash
gh workflow run ios-mobile-captures.yml --ref master -f source_sha="$MERGED_SHA"
```

The artifact `ios-mobile-captures-<source_sha>` contains the `.xcresult`, exported
attachments and their export manifest, the exact source SHA, and a capture
manifest with SHA-256 hashes of every built bundle file. Each successful matrix
cell attaches one screenshot named `mobile-<state>-<language>-<theme>`:

- states: `default`, `offline-open`, `open-failed`, `stale`, `account`, `first-run`;
- languages: `zh`, `en`;
- themes: `light`, `dark`.

There are **24 named screenshots** on a successful run. A state/readiness
assertion failure fails the run; partial artifacts remain diagnostic evidence,
not an accepted capture set. Review the screenshots and the actual test result
before sending them to Product. The source/expected-name manifest alone is not
proof of test execution.

## How states are reached

Only `AvibeSheetFixture` reads `AVIBE_FIXTURE_CAPTURE_STATE` and
`AVIBE_FIXTURE_CAPTURE_THEME`. No production launch flags, bridge overrides,
replacement HTML, injected state-setting JavaScript or production test hooks
are added. UIKit controls the fixture's appearance; standard Apple language
launch preferences control the bundled page's browser language. Each cell
launches fresh and clears only the fixture's defaults domain.

The fixture uses a deterministic native fake transport with the real session
coordinator and compiled bridge. It preloads an authorized synthetic account
and selects Studio for sheet states; first run has the same account and no
selected instance and is presented full-screen. Home is offline. The offline
expansion and Account page are reached by real XCUITest taps. The stale banner
comes from a failed real bridge refresh after the initial native bootstrap.
The open-failed state taps Lab, whose synthetic catalog URL is deliberately
non-HTTPS: the real coordinator rejects it with `invalid_instance`, and the
real page renders its generic row failure. This proves the row-error state,
not an actual runtime/network connection failure. No remote request is made.

The test waits for native refresh consumption and state-specific localized
content before attaching screenshots. The static gesture page cannot satisfy
the real-page assertions. Routine `ios-shell` builds/typechecks the capture
class but skips its execution; its four existing gesture tests continue to use
the original static document. The capture workflow selects only the capture
class.

## Safety and acceptance

Run in the isolated CI simulator, not the owner's live desktop. All account,
instance and host values are synthetic (`example.invalid`); there is no real
login, push registration, credential, mnemonic, keychain, clipboard operation
or external browser action in the matrix. No secret-bearing field is populated
in these six states, so no credential masking is needed before saving these
synthetic screenshots. Reports must explicitly distinguish this from a test
with real credentials or any clipboard exercise.

Simulator captures do not establish real-device gesture feel or TestFlight
acceptance. The owner still accepts the real-device build, and the native
full-screen fallback remains until then. This workflow cannot promote, sign,
ship or enable a feature.

## Validation ownership

- The XCUITest is the owning guard for the real rendered state, localization,
  interactions and screenshot matrix. It intentionally fails on a bundled UI
  that does not implement the approved sheet states.
- The workflow-contract test prevents automatic PR execution, moving-source
  checkout and loss of real-bundle build/copy or attachment export.
- Manifest tests bind exact built bytes and all 24 expected names, reject
  abbreviated/moving refs and static-only/symlinked input. They do not claim a
  screenshot has executed.
