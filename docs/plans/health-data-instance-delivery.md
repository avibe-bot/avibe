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
The private mobile storage tree is also excluded from generic file reads,
recursive copy/move/export, registered media, and Show serving. The general
same-origin Show/file-browser authority model is outside this change.

The existing instance host fence stays in force. Device routes bypass cookie
and CSRF authentication only after exact method/path and raw-path matching;
their native handlers authenticate the code or bearer and recheck live Personal
ownership and an empty allowlist. Offline projections cannot authorize sync.
The existing Cloudflare ingress forwards the instance hostname unchanged; no
backend code or production gateway deployment is required.

The store uses a separate versioned SQLite file with mode 0600. Cleanup removes
health days after 30 local-calendar days, terminal requests after seven days,
and access records after 90 days; revoke, sharing, ownership change, sign-out,
and unused credentials purge device health data. Controller maintenance runs off
its event loop. OS waiter locks and transactional continuation claims give the
synchronous waiter or a stable callback Run sole continuation ownership.

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
  This includes the canonical `AUTH-SETUP-180` closed-loop scenario and the
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

Authorized: implementation and a non-draft English PR through Merge-ready.
Not authorized: merge, install an S1 package locally, modify Apple App ID or
profiles, TestFlight, release, production service / state / push or remote access.
Real-device acceptance belongs to the orchestrator's approved integration pass.
