# Hermetic integration and distribution checks

Use the repository's existing pytest environment (`uv sync --no-install-project`),
Node supported by the Runtime manifest, and a real `avault` custody binary on PATH.
The fixture exclusively uses a synthetic PAT, a temporary file custody store,
temporary HOME/XDG/AVIBE_HOME, app ledger and loopback GitHub HTTP server. It does
not use Keychain, installed user secrets or live GitHub writes.

Run focused tests:

```sh
PYTHONPATH=. .venv/bin/pytest -q tests/test_feedback_intake.py
```

Obtain the normal deployment `show-runtime-manifest.json` and the archive for the
local test platform from the accepted release artifact. Set absolute paths:

```sh
AVIBE_FEEDBACK_RUNTIME_MANIFEST=/absolute/path/show-runtime-manifest.json \
AVIBE_FEEDBACK_RUNTIME_ARCHIVE=/absolute/path/vibe-show-runtime-node-linux-x64.tgz \
PYTHONPATH=. .venv/bin/pytest -q tests/test_feedback_intake_http.py
```

The test verifies the fixed manifest SHA, Runtime version 5e31 and platform archive
SHA against exactly the executing host platform before extracting into temporary state.
The app imports `runtime_platform_tag` and `safe_extract_tar` from the normal
installed Avibe package; standalone invocation needs that Python environment
(or `PYTHONPATH=.` in the repository). It never executes an unverified archive. Linux x64 must match `ea1079eca7…`.
macOS uses its entry in the same frozen manifest. Platform-specific native modules
require the matching archive; a macOS pass is not Linux deployment evidence.
The existing core CI pin 5a9a6a52 remains untouched.

`vibe-fixture.py` invokes the real Avibe CLI and real avault delivery. Its only
custody adaptations select the test binary and file store, avoiding automatic
macOS Keychain use. Actual named secret resolution, encrypted envelope stdin,
start_new_session semantics and child lifecycle execute unmodified product code.
The report is read from its app ledger, not Vault stdin. No exec-style fake Vault
stands in for the CLI. Production must never install this fixture.

The HTTP case runs actual uvicorn public admission, packaged Runtime, app TS,
worker, real Vault CLI, GitHub fake and receipt GET. It checks Unicode/newlines,
status-only POST, repeated/conflicting ID, malformed encoding, size/method,
minimal status privacy, DB/WAL/SHM route probes, proxy exclusion and a completed
upstream write followed by a disconnect. Focused tests cover quotas, capacity,
real concurrency, process death, bounded read-only reconciliation and helper
resume/deduplication. Real custody tests skip without avault, and HTTP skips
without artifacts; those skips do not satisfy commissioning.

For normal wheel publication, build the UI and normal wheel as in the app README.
`tests/test_feedback_intake_distribution.py` accepts `AVIBE_FEEDBACK_TEST_WHEEL`
and verifies helper bytes from the wheel through installed-source selection and
built-in snapshot publication. Normal artifact installation remains PM/ops-owned.

Recovery/crash consumers: `tests/test_feedback_intake_recovery.py` runs actual
helper subprocesses, real HTTP receipt/admission and the production app SQLite
reserve/claim path with an isolated GitHub write counter. It kills at durable
intent boundaries and races original/recovery delivery without another Issue.

Artifact consumers: `tests/test_feedback_intake_artifacts.py` runs normal and
optimized Python, platform/hash rejection and safe/malicious fixture extraction.
Set `AVIBE_FEEDBACK_LEGACY_PYTHON` to an isolated Python environment containing
Avibe dependencies on an actual older interpreter without `tarfile.data_filter`
(e.g. Python 3.10.11) for the legacy compatibility check. It must not point at a
production service environment. The modern path and existing
`tests/test_managed_runtime_composite_artifacts.py` remain useful alongside it.
