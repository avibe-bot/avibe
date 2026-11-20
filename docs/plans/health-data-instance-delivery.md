# Health data instance delivery contract

Baseline: `origin/master` at `22b9a0259af20c092294363ffc02ec2155fc7a39`, fetched
with the explicit `+refs/heads/master:refs/remotes/origin/master` refspec.
Authoritative product contract: `health-data.md`, including amendments 1–3.

## Intended behavior and boundaries

An owner can hand a five-minute request-bound code to their iPhone. Only that
phone's device credential authorizes the exact device API routes. Daily uploads
replace complete windows monotonically; empty and missing components remain
explicit. Local Agents read only the four approved data groups from independent
private storage. No browser API returns health values. Request results, cards,
callbacks, access records, and logs contain metadata only.
The private mobile storage tree is also excluded from generic file reads/copies,
registered media, and Show paths. Recursive copy and cross-filesystem move
fallback validate descendants before publishing. Ordinary delete and
same-filesystem move retain their existing semantics. The general same-origin
Show/file-browser authority model is outside this change.

The existing instance host fence stays in force. Device routes bypass cookie
and CSRF authentication only after exact method/path and raw-path matching;
their native handlers authenticate the code or bearer and recheck the live
instance owner on any instance kind or sharing state. Offline projections pause
sync without purging.
The existing Cloudflare ingress forwards the instance hostname unchanged; no
backend code or production gateway deployment is required.

The store uses a separate versioned SQLite file with mode 0600. Cleanup removes
health days after 30 local-calendar days, terminal requests after seven days,
and access records after 90 days; an explicit revoke or unpair, a live
confirmed ownership change, and unused credentials purge device health data.
Sharing does not (November 20 decision). Controller maintenance runs off
its event loop. OS waiter locks and transactional continuation claims give the
synchronous waiter or a stable callback Run sole continuation ownership.
These retention rules concern active mobile storage; they do not erase
conversation transcripts, model contexts/caches, historical backups or
provider-side copies. No system backup configuration is changed.

Delivery and listing follow pairing (#3989). Commits that can owe a
continuation (a device result, a cancellation, a released waiter, pair or
revoke) publish the existing `mobile-health` scheduling hint, and the
controller delivers on it. A sweep that runs once a minute while a device is
paired (or a continuation is owed) handles what no commit announces: expiry, a
crashed waiter, a lost hint, cleanup and eligibility. Controller startup
delivers anything still owed. An instance with no paired device and nothing
owed has no Health job. The built-in `use-mobile-health` Skill declares
`requires: mobile-device`, so it appears in Agent Skill catalogs only while a
device is paired; it stays loadable by name, and `vibe --help` still lists
`mobile`. A pairing change applies the next time a prompt is built.

## Verification

Use only synthetic values, fake native push, fake control-plane responses, and
a fake device over in-process ASGI. Pytest redirects HOME, all XDG homes,
AVIBE_HOME, backend homes, ports and databases and rejects unintended network
access. Cover credential scope, pairing replay, complete day replacement,
sequence ordering, lease reclaim, late results, timeout data, one continuation,
nullable sleep, retention, owner/shared policy changes, and general-query
isolation. Parse every shipped Skill command with the real CLI parser.

Separate security review is assigned by main after implementation. The PR must
pass exact-head Codex review, all applicable CI and have zero unresolved threads.

Local evidence before PR:

- 26 focused cases cover fake-device pairing and status, hostile input,
  route-scoped remote bearer authentication, private file/media/Show boundaries,
  wait/callback ownership, push filtering/budget, retention and CLI contracts.
  This includes the canonical `AUTH-SETUP-186` closed-loop scenario and the
  controller maintenance lifecycle.
- 707 adjacent Show, CLI invocation authority and instance operation cases pass.
- Adjacent message mirror, file browser, media, Web Push and scenario citation
  cases pass; the preexisting filesystem-root delete refusal keeps its original
  error code.
- Changed Python files pass Ruff; Skill examples use the real CLI parser.

Review follow-up on `63e1212e18a4fbc73f48aa784b9a1052d458c931`:

- Codex identified initial-delivery compensation and optional empty DELETE
  bodies. Main confirmed both fixes. Eight regression cases first reproduced
  the failures, then passed with cancellation of only a newly created request
  and parsing of actual body bytes. Connect and refresh both cover a concurrent
  coalesced caller and a fresh retry; failed delivery consumes its continuation.
- The route and built-in Skill inventories now include mobile. The private-path
  canonicalizer preserves ordinary cyclic-link metadata/removal on Python 3.12
  while content readers still require a resolvable path. A consuming test
  simulates the older interpreter's cycle behavior.
- Independent security review found a macOS filesystem-alias bypass in the
  original string comparison. Private roots, descendants and ancestors now
  compare device/inode identity as well. This covers firmlinks, direct `/.vol`
  file aliases, hard links and literal percent characters. Directory listing and
  search reuse one operation's root and descendant snapshot. Synthetic macOS
  tests exercise read, list, copy, move, write, delete, media and Show refusal.
- Unknown, used and expired pairing codes are rejected before a live control
  plane query; the transaction still revalidates and consumes the code. File
  creation also supports Python without descriptor chmod and rejects links
  before opening.
- 293 focused and adjacent cases pass, including private storage, file browser,
  role inventory and Prompt Studio. Together with the fake-device closed loop
  and controller maintenance lifecycle, 39 health-specific cases pass.
- An explicit refspec fetch and rebase onto
  `d5547843a0f156420461e67d19ca61320b6d8fcb` incorporates the independently
  maintained root-help compaction in PR #3930. All 296 focused, adjacent and CLI
  help checks pass after the rebase, including the unchanged 2.5 KiB help budget,
  English-only CLI text and parser-backed examples. Only mobile's own
  description is shortened here.

Review follow-up on `a2fd5038a5f669792ecec5eba5e9ece9dd904359`:

- Exact-head Codex passed, both original threads were resolved, all 49 CI checks
  passed or were explicitly skipped, and GitHub reported `CLEAN`.
- The main-assigned independent security delta review closed the original
  findings but found an operation-wide failure state after one inaccessible
  public entry, and alias preplanting while the private root did not yet exist.
  The repeated storage-boundary class triggered the review circuit breaker.
  Main independently diagnosed the scope and approved these two corrections.
- Private-root or snapshot failures still refuse the entire operation. A single
  target/ancestor failure now refuses only that target, preserving other listing
  and name/content search results. An absent private root is identified by its
  nearest existing ancestor's device/inode and remaining path components,
  including reverse ancestor checks. Inspection creates no mobile directory.
- Five new regression cases include real macOS firmlink mutations against a
  missing root and a synthetic mode-000 public symlink. The relevant new failures
  were reproduced before the fix. All 303 focused/adjacent/CLI/scenario/lifecycle
  cases and 707 Show/authority/instance-operation cases pass.
- Waiter-lock cleanup is tracked separately by main. Mounting-container traversal
  remains subject to main's independent scope assessment; the copy-size limit is
  not an authorization boundary. Independent review of this new storage delta
  remains required.
- Max subsequently selected an owner-origin read policy: any instance Agent may
  serve the owner, while group, other-user and unverified origins must be refused.
  The source-verification design is under main's decision separately from this
  storage correction. Current CLI process identity alone does not establish
  human ownership, and same-user arbitrary shell access is not isolated from the
  database. This policy is an outstanding delivery gate.

## Current-instance repair scope (November 18, 13:18)

Max's 13:10:41 decision selects the existing instance and supersedes the
separate-instance deployment route. The supported configuration inspection found
no enabled external IM, no bindings on all five platforms, no additional live
allowlist grants and a loopback UI listener. This current owner-only configuration
is the acceptance target; future sharing/IM changes require another assessment.
It is not proof of a hard execution boundary or acceptance of all residual risks.

The recursive-copy finding on `a0f3eb51d72cae86561084362f697372fd49132b`
and the independently reproduced descriptor-alias exposure are still open before
this repair. Three findings-bearing heads in this root class stopped lane
patching. Main diagnosed the complete inventory and authorized this finite
repair, with one commit/push and another main-owned security delta review:

- Deny original, symlink-intermediate and resolved descriptor namespaces through
  the existing file/media/Show gates. Preserve private-root identity, absent-root
  anchors and target-local failure behavior.
- At file download and copy input opens, check `fstat` against the operation's
  private identity snapshot, then consume that same descriptor. No destructive
  open or output precedes this decision.
- Reuse checked measurement/traversal for recursive copy and EXDEV fallback.
  Recheck during actual consumption, including symlink targets. A protected
  descendant rejects the operation without publishing a partial destination or
  removing the source; clean only this operation's stage and record its path if
  cleanup fails. This is not an atomic transaction against concurrent processes.
- Keep the copy endpoint's size budget, without adding it to EXDEV moves.
  Snapshot construction is O(private entries); subsequent identity membership is
  O(1) per visited source entry, with O(private entries) snapshot memory. There is
  no configured upper bound on the number of private entries scanned.
- Consumer regressions use real POSIX descriptors and nested private hard links,
  symlinks, late insertion after measurement, unchanged overwrite destinations and
  source preservation. A normal 400-file tree must still copy/move. Linux fd and
  map_files checks require actual Linux CI; macOS skips do not satisfy that gate.

Trusted original-owner provenance across delegation remains **unimplemented**.
CLI environment caller/session labels are audit data, and live instance
eligibility is not human-source authentication. This repair does not implement
source infrastructure or isolation deployment. It does not block arbitrary
same-OS code, terminals, author-controlled Show code, preexisting copies, or new
private inodes hard-linked outside the tree after the snapshot. These limits
must stay visible in delivery reporting; option C is not claimed complete.
Another uncovered counterexample in this boundary class stops further patches
and returns the full inventory to main.

Local repair evidence:

- Loading the three original consumer modules from `a0f3eb51` with the new
  synthetic tests reproduced 25 failures, including HTTP 200 descriptor reads,
  media registration/serving, Show serving and recursive private-link export.
  The current consumers pass all 29 focused repair cases on macOS; two real
  Linux procfs cases remain for CI.
- All 1,095 health, file-browser, media, Show and ACL consumer cases pass on
  macOS, including prior firmlink, `/.vol`, percent-root, missing-root and
  target-error cases. `AUTH-SETUP-186` also passes independently with the real
  MobileStore, SQLite, CLI parser/executor and ASGI device routes; only the
  phone, control plane and push transport are synthetic.
- Main's bridge decision (a) is reflected in `health-data.md`: connected means
  committed connection; synchronization success still requires upload ACK and
  server-accepted receipt. No device protocol or lease change.

### Namespace and metadata follow-up (November 18, 14:01)

Main's independent reviewer passed `9b4affa1dc5dba582049e3010746d5b278948c08`
within the finite scope above. Main authorized one follow-up for two findings:
case-insensitive descriptor namespace spellings and EXDEV metadata preservation.
The same metadata regression was independently reported by Codex on that head.

- Normalize namespace spelling before checking raw and intermediate paths.
  Search/replace text readers also validate `fstat` before reading the same fd.
- EXDEV staging retains `copystat` behavior, including supported timestamps,
  flags and extended attributes. File and POSIX directory metadata come from
  the verified source fd. A metadata-copy failure leaves the source in place,
  refuses publication and cleans this operation's stage.
- Ordinary copy's metadata behavior and size budget remain unchanged.
- The verification runner and its import helpers live in a dedicated private
  temporary directory; the whole `/tmp` directory is not on `PYTHONPATH`.

Main's 14:09 decision also includes Codex's abandoned synchronous-waiter defect:
`arm_wait` may replace an existing pending waiter only after its reservation
expires and an OS lock probe establishes that its lock is free, within the same
write transaction. A held, missing or unverifiable lock does not establish
abandonment. The request state/result, callback destination and one-continuation
ownership remain unchanged. A late old waiter cannot consume the replacement.
Real subprocess-death, active-lock, unexpired-reservation, concurrent-reclaimer
and CLI/result/callback regressions cover this correction.

Authorized: implementation and a non-draft English PR through Merge-ready.
Not authorized: merge, install an S1 package locally, modify Apple App ID or
profiles, TestFlight, release, production service / state / push or remote access.
Real-device acceptance belongs to the orchestrator's approved integration pass.
