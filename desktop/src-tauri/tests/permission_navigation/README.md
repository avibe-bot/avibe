# Permission navigation regression

This test runs the macOS controller queue and event loop without creating a
Tauri/AppKit app or calling the OS. `run.py` extracts the product methods without
rewriting their bodies; the fixture replaces only platform/process adapters.
Its dependency is the real `desktop/runtime-host` lifecycle in the selected
source tree. `source-manifest.json` records copied-item hashes.

The owning contract is **Open Settings never requests permission or captures**.
The credible regression is reconnecting either pane handler to an authorization
request. Existing UI tests only observe native `invoke`, so cannot catch that.
The fixture consumes actual `Controller::open_permission` queue events and
`RuntimeState::run`, `open_permission`, `activated`, and setup marker logic.
It also executes the real enable/menu request dispatcher as a positive control.
OS recorders never grant permission. The simulated driver acknowledges a real
lifecycle start directive; this does not constitute a real-driver/TCC test.

Run independently of `cargo test -p avibe-desktop` (which does not discover this
same-source assembly), from the repository root:

```bash
EVIDENCE="$(mktemp -d /tmp/avibe-permission-navigation.XXXXXX)"
python3 desktop/src-tauri/tests/permission_navigation/run.py \
  --source "$PWD" --output "$EVIDENCE/fixed" --target-dir "$EVIDENCE/target"
```

Dependencies must already be cached: Cargo uses `--offline`, one build worker,
no incremental cache, and no debug symbols. The assembly seeds its resolution
from the source workspace's `Cargo.lock`; it never edits that lockfile.

To establish the regression on an unmodified earlier controller, export that
revision's `computer_use.rs` and pass `--controller-source /path/to/baseline.rs`.
Keep the same fixture and runtime-host input for both runs. On the pre-fix
controller, the two-pane negative cases record Accessibility/Screen Recording
requests and capture; the enable/menu positive control still passes. On the
fixed controller, all four tests pass, covering 48 navigation combinations
(two panes, four grant states, three Runtime support states, enabled/off), two
consecutive clicks, activation recovery, and marker persistence/settlement.

Unknown native Runtime support is not modeled as a granted permission. The
underlying macOS permission-preflight API returns booleans; unreadable/unknown
Web status is covered separately by `SettingsComputerUsePage.test.tsx`.
Real signed-package acceptance remains a separate, owner-scheduled check.
