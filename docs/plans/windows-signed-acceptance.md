# Signed Windows Computer Use acceptance (draft-only)

This harness belongs to one **draft PR that must never be merged**. It consumes
an already-published, signed Windows x64 installer, never builds or signs a
release, and never receives release/model credentials.

## Change contract

- Run only from this same-repository draft PR on GitHub-hosted `windows-latest`
  (Windows Server 2025 x64), with a 45-minute total runner budget.
- Download the installer and its release provenance. Verify SHA256SUMS and
  Windows Authenticode trust with a Certum issuer before executing it. Compare
  the installed `computer-use/cua-driver.exe` to both Stage 2 provenance hashes.
- Install, launch and uninstall under a newly created Users-only local account;
  never treat a filtered administrator token as a standard user.
- Preparation uses `gh-v3.2.7rc3` and asserts Computer Use remains unavailable.
  Preparation passing is not enabled-feature acceptance.
- Enabled acceptance must use the shipped CLI/JS path and the actual Settings
  enable action, without an LLM Turn. No model credentials are available.
- Keep only value-free JSON and per-case Markdown for seven days. No screenshots,
  clipboard capture, user text, credentials, raw process output or state trees.
- Yield to RC/stable/acceptance release workflows. A blocked or unexecuted case
  is NOT RUN, never PASS.

## Required enabled cases

1. Launch the pinned official portable Notepad++ Win32 editor by
   `computer.app({path})`, attach, type synthetic
   unsaved text, then independently exercise CU off, Desktop quit and Desktop
   crash. In each case the original app and its unsaved text survive while the
   driver and owned helpers exit.
2. Refuse bounded control of a verifiably elevated app with `protected_window`
   (metadata/attach alone is not prohibited), and refuse service from an elevated
   driver. Unrelated errors such as `unverified_window` are not a PASS.
3. A second local user cannot open the first user's pipe or acquire its lock.
4. Locked desktop and a true second interactive session: report NOT RUN if the
   hosted environment cannot demonstrate these honestly. A second account or
   noninteractive logon is not a second interactive session.

Product requires the real LLM Agent Turn to remain **NOT RUN** on hosted
runners, deferred to the owner's ten-minute real-machine check before Preview
can be removed. The activation RC must be explicitly selected only after its
activation PR has landed and its installer/Stage 2 provenance are published.

## Signed-run stop-loss and dry gate

The two enabled attempts (`43701888417`, `43706480357`) did not retain the
originating enabled-operation result. They establish a harness observability
failure, **not** a product failure or product pass. The enabled product outcome
remains **UNKNOWN** for their missing operations. The subsequent 48-case hosted
dry gate and independent review passed before signed run `43724343207`. That run
retained the real launch/type returns and passed native unsaved-text readback,
but stopped at helper inventory before CU Off. No lifecycle/security PASS follows.

The inventory correction keeps the shipped lifecycle: a persistent Desktop-owned
installed daemon plus same-image runtime MCP proxies, not per-call or temporary
copies. CIM supplies candidate names and parent links only: its `ExecutablePath`
property has a documented debug-privilege qualifier, unsuitable as an existence
oracle for a Users-only worker. The bounded follow-up uses `Get-Process -Name`
and same-user `.Path`, then compares the installed path/hash and queries token
SID on retained handles. Bounded candidate
rows record basename, nullable path/hash/owner matches, parent relation and error
class without full paths. This diagnoses future misses; it does not prove which
field caused the earlier miss. Replacement/descendant scans use the same observer.

The workflow is **dry-only** while this correction is verified. The first new
ordinary-user control (`43726782304`) exited nonzero without its inner error
receipt; that setup/query failure remains unclassified. The single follow-up
control records worker phase/class/exit before aggregation. If it still cannot
verify identity, the explicitly authorized `HELPER_IDENTITY_OPTIONAL` switch may
allow the signed run to execute app-survival/security cases anyway, retaining
`helper_identity: NOT VERIFIED` and never claiming helper exit/ownership PASS.
A false `helper_identity_verified` keeps the overall acceptance gate false even
if the separately observed product cases pass. The precise target HWND/text and
security refusal predicates remain mandatory; uncertain processes are not killed.

The authorized sequence is one hosted dry run, then one further signed attempt after fresh
release admission; independent review runs in parallel. Only the explicit mode
change enables that attempt. This PR never merges.

The dry fixture stages the harness through the same `Copy-AcceptanceHarness`
manifest as the ordinary-user worker, then invokes `Invoke-EnabledCases` from the
staged `enabled.ps1`. An invalid-SID bootstrap test loads the staged `user.ps1`
and its helpers, stopping before installation or Desktop/runtime effects; a
missing-helper negative control must fail that bootstrap. It then
generates its launch/type JS and Windows PowerShell wrapper, obtains the outer
tool command from the actual `model_stub.py`, and executes that command. On
Windows the wrapper runs in Windows PowerShell 5.1 and an inert compiled console
executable provides real argv/stdout/stderr/exit boundaries. The generated JS
runs in a persistent, test-owned V8 evaluator with inert computer/window methods.
The same case reader, diagnostic persistence, worker merger and required-case
gate produce the final JSON. `InputOnly` returns a **dry-only** verdict before
any target-process/native lifecycle acceptance; no dry result is product proof.
The real `Native.cs` is compiled under PS5.1. A separate dry positive control runs
its query-only process identity/retained-exit methods against a test-owned inert
executable under a newly created Users-only account, without elevation or process
ACL grants. The account/profile/processes are cleaned up. No UI/native input
methods or shipped product processes are invoked by this dry control.

Evidence boundaries and fault ownership:

| Boundary | Evidence before advancing | Fault treatment |
| --- | --- | --- |
| Settings / script generation | fresh operation ID and initialized nullable diagnostic schema | preserve original call/class/line before outer abort |
| Genuine Turn / generated tool dispatch | operation-bound dispatch receipt and envelope | numeric exit, start/wait/parse error; no raw stderr |
| Wrapper / native observer load | wrapper receipt before Add-Type | no missing field interpreted as success |
| CLI launch and type calls | separate call records, actual exit and sanitized CLI code | empty/malformed/wrong envelope fail closed |
| JS app/launch/wait/observe/activate/type | fixed stage, first error, attempt count | no retries; post-observe cannot mask type failure |
| Exact-HWND native focus | nullable observation, then single background attempt | false/error suppresses typing; lost CLI output means attempt unknown |
| Receipt/result handoff | matching operation ID, atomic files, one result writer | missing/stale/mismatched evidence cannot satisfy the gate |
| Native identity/readback/lifecycle/security | named stages and original failure retained | product behavior remains unproven in dry mode |
| Worker/parent cleanup and aggregation | immutable original error plus separate cleanup errors | failed worker or missing required case cannot become green |

Receipt persistence is not the only evidence channel: the wrapper always emits
its sanitized receipt in its final stdout envelope, and the dispatcher does the
same even when its result write/rename fails. The real model fixture records an
allowlisted projection of that tool output in the operation-bound stub receipt.
The collector may recover known diagnostics from it, but never forgives a failed
write, missing result, or stale/malformed receipt. End-to-end injected write and
rename failures must retain the original CLI exit 37 and code, not replace them
with the later persistence failure. This fallback is evidence only, not a second
writer of the case result.

The earlier source-string readiness test was insufficient and has been replaced
with an executable behavioral fixture. Local portable-PowerShell runs are useful
regression evidence, but **do not** satisfy the hosted-Windows stop-loss gate.
No owner Desktop, clipboard or keychain is accessed; no screenshots are saved.

## Known by design: rc3 preparation status surfaces

The rc3 UI capability route returns `unsupported_host/unavailable` before reading
shared effective status. The kernel independently reads `effective_computer_use_status`,
which returns `off` for a never-enabled/disabled state, and checks that off guard
before its platform-support branch. Therefore preparation intentionally requires
both the public disabled-capability response and the genuine Turn's strict
`computer_use_unavailable` with `params.status=off`; it does not broaden the
probe to accept a generic platform refusal. This ordering is present in rc3's
published source `492fee198f643a2deb14277670b9906f263c82a9` and was exercised by
signed preparation run `43689203739` with the same strict assertion.

## Selected enabled candidate

The authorized enabled run selects `gh-v3.2.7rc4`, source
`bcc71bc8d6430fc72697eb4695caef5d15da55ba`, after Stage 2 run `43695839638`.
Its candidate driver is **34,074,624 bytes**, SHA256
`931310cd70e4c571af416c77cdce56719bda3e1e1240ba4128ce913610b0294e`.
The workflow supplies explicit candidate commit/hash/size pins. The verifier
binds the public SOURCE fields to these pins, then checks the actual installed
bytes and `computer-use/driver-provenance.json`; rc3's hash is not reused.
For the later update mode, these same candidate pins describe the target rc4,
not its starting rc3 installation. Candidate selection is not an acceptance
result; required enabled cases must actually execute and pass.

## Evidence and current preparation

The first full rc3 preparation passed in run `43670974577` on head `a08ef4cdb`
(Windows Server 2025 x64, about 2 minutes 35 seconds). The installed driver hash
was `7d64933b79355e336b915461a501ebf187b5c975869f99e9d57f45a5faf3f8a7`,
matching both Stage 2 prepared and packaged hashes. This is **not** enabled
Computer Use acceptance. Its optional WebView2 loopback CDP preflight was then
NOT RUN/unproven.

Expanded preparation passed in run `43677783571` on head `3a28c6407`. In addition
to those checks, the actual installed WebView2 accepted loopback CDP, a visible
Settings rail link was clicked, and the resulting Computer Use DOM was read.
The real native Quit menu and Keep Runtime running dialog also completed. This
is not a Settings **enable** test: rc3 still has the feature disabled.

Run `43684652439` on `0dc490105` repeated all preparation/editor/query checks
and the actual Settings DOM click successfully. Its unified-panel preflight
clicked VersionBadge and Check and required a distinct visible checked-at
receipt, a successful settled state, and Current version `3.2.7rc3`.
Auto-update remained false and prereleases true. No Update was clicked; this
proves the shared UI route, not an installation transition.

The supported credential-free tool-context fixture is now proven through the
default-off rejection: the real
installed Model Hub points to a tiny loopback OpenAI-compatible deterministic
server, and an ordinary `vibe agent run` starts the real shipped Vibey Turn.
The server asks the actually advertised Windows `powershell` tool to run the
shipped `vibe computer js`. It does not fabricate Session/Turn IDs, write the
runtime database, replace the Hub engine, or modify installed code. This is
reported as **deterministic model stub, not an LLM**. Only the external observer
judges case results; the stub never declares acceptance PASS. The nonempty
source-key field is a fixed fixture placeholder, not a model credential.
The prep probe requires the exact shipped CLI `computer_use_unavailable` error
with status `off`, reached after live-Turn admission; JS is **not executed**
while off. Enabled action execution remains NOT RUN until the activation RC.

Security fixture reports preserve each completed case independently of later
fixture failure. Lifecycle observations retain original process handles and
also scan for owned replacement driver/helper generations across the native
reprobe interval. Cleanup independently attempts process, profile, account,
port and work-root steps and verifies account/profile removal.

The elevated-target fixture additionally needs a positive control from the
actual Users-only caller, reopening the target process/token by PID. A narrow
candidate grants that SID only process-query and token-query object rights on
the freshly created elevated editor. Existing ACEs, integrity, identity and
privileges stay unchanged; shared target/runner token objects are refused.
Native run `43680355580` proved this narrow positive control on the pinned
editor: the actual Users-only caller reopened the elevated target, and the
parent's retained-target postcheck passed. This is query feasibility only,
not a protected-window control PASS. Failures remain
`elevated_target_query_access_unproven`.

Run `43678884845` found that Server 2025's System32 Notepad exited/rebrokered;
the query-ACL positive control was therefore **NOT RUN**, not disproved or
passed. Additional real-editor fixture preparation started at 13:06 UTC+08 on
November 15, independently of the already-completed model-stub timebox. The
replacement fixture pins official portable Notepad++ **8.9.9.1** (archive SHA256
`cbcefa34677e10fe6dd19802f7d6b3b9860cd6de62afeaae7680e09c51b47779`). Native runs
record its public source, actual executable hash/version, Authenticode signer
identity and validation. The executable directory is read/execute-only to the
ordinary account; each launch uses separate test-owned settings with plugins
and session reuse disabled. No name/descendant fallback may replace the exact
created PID. A prep-only ordinary-process/blank-document readback and an
elevated query positive control are separate from actual CU typing/refusal.
The observer reads one visible Edit/RichEdit/Scintilla document control with
`WM_GETTEXT`; it never writes document contents. Run `43680355580` also proved
the standard-user exact-created-PID/window/blank-document preflight and
recorded executable SHA256
`5518ad65f576b993c48da1878bda83a62f7a101d246aefab472379ca8800390c`, with a valid
timestamped `NOTEPAD++` signature. Additional fixture preparation took about
17 minutes (13:06–13:23 UTC+08). Nonempty CU typing and elevated control remain
NOT RUN on rc3. That run's Settings DOM readiness timed out; the earlier CDP
proof is not substituted for a fresh per-run prerequisite.

Settings is an on-only **Turn on button**, not a switch. The native menu owns
Off and Quit. Settings automation must click the shipped button in the actual
installed WebView; the harness may not substitute a direct native invoke or a
configuration write. Native menu automation discovers visible menu items and
uses their normal Windows menu-command route. No screenshots are written.

## Unified update panel (signed rc5 → rc6, outcome-only)

The explicit `update` mode installs the signed base release under the ordinary
account and updates it through the real unified panel only: VersionBadge →
`[data-update-panel]` → Check → the exact public offer → Update. Only the pair
selected in `run.ps1` is allowed (rc5 → rc6). Both releases pass the same
checksum/Certum/provenance verifier; the target installer is reference-only and
is **never executed by the harness**. The pre-release switch is clicked only if
it is off; auto-update must not change. CU/model/editor fixtures are skipped.

After the click, the case judges only user-visible outcomes (Product decision
after two process-observer attempts). It does not inspect the update supervisor
or installer (`update_installer` is NOT RUN by design and not required):
1. within 3 minutes a new same-user Desktop window at the installed path, started
   after the click, appears;
2. 60 seconds later exactly one installed-path Desktop runs, it is that new
   windowed process, and the original has exited; two running is
   `update_product_double_launch`, and the original still running is
   `update_original_desktop_not_exited`;
3. the Desktop file version, runtime version and installed driver
   (hash, size, provenance) equal the target;
4. the panel reopened through the badge shows the target as current, with
   auto-update and pre-release unchanged.

Plain same-user process listing (`Get-Process` path/start time) is used; there
are no token or module reads of updater processes. Any exception keeps its
original class and line, and the observations are written in `finally`. On
failure, worker uninstall is suppressed and the parent removes the account.
Signed run `43756149490` passed this case.

Downloads recheck release admission and have at most 180-second timeouts. A
40-minute operation deadline and a bounded worker leave room under the
45-minute job limit for owned-process/account cleanup and seven-day artifacts.
