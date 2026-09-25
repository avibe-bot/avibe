# Model usage analytics

Status: implementation.

## Approved behavior

The owner approved the interactive usage preview on September 24, 2026, including
the subsequent explicit pin interaction. This approval is the visual reference
for the new usage surface; existing Model Hub tokens and primitives still own
the surrounding shell. The reference is the orchestrator Session's Show Page
(`seskugjeceav3`), not a new navigation shell to copy into the product.

- A shared time range and multi-select Source/model filters drive the statistics,
  chart, table, and CSV export.
- Offer 24 hours, 7 days, 30 days, and 60 days. The initial view is 24 hours.
  Use hourly lines for 24 hours, daily lines for 7 days, and daily stacked bars
  for 30/60 days. Display the server-returned time boundaries and time zone.
- The hourly view includes the current partial hour and preceding 23 hourly
  intervals. Label the partial hour and disclose the actual range. It is an
  hourly-bucket view, not an exact second-level sliding window.
- Metrics: all tokens, input including cache, output, cache reads, requests.
  Groups: total, token type, model, Source (labelled supplier in the UI).
  Token-type grouping is unavailable for request counts.
- Total tokens = input + output. Disjoint token series use noncached input
  (`input_tokens - cached_input_tokens`), cached input, and output.
- A model's identity is `(source_id, model_id)`, never its label. Different
  configured Sources remain distinct even when labels or vendor brands match.
- A hover opens readable exact figures for its time bucket. Clicking a point
  opens an ordinary, dismissible detail popover. Outside click closes it.
  Only the explicit pin control in the popover corner pins/unpins the detail;
  clicking the plot again never toggles pinning. Pinned details survive outside
  clicks; changing report scope clears the pin. Escape dismisses the detail.
  Pinning also narrows the table to that bucket; unpinning restores the range.
- The chart precedes the exact-count details table. Chart legend visibility is
  presentation only and must not silently change report scope or CSV contents.
- Preserve honest unavailable/partial-report states, historical identities,
  loading/failure behavior, keyboard access, light/dark themes, and narrow screens.

## Shared wire contract

The existing `GET /api/models/usage?days=N` remains supported with its current
shape and default. The same route accepts an exclusive `window` selector:
`24h | 7d | 30d | 60d`. Invalid or conflicting selectors return 400.
The controller RPC, its client, and service forward this selector explicitly.
The modern UI uses `window` exclusively.

A modern response retains `{ok, contract_version, usage}`. `usage` extends the
existing UsageSummary fields with the following required fields:

```ts
type UsageWindowKey = '24h' | '7d' | '30d' | '60d';
type UsageBucketRow = UsageCounters & {
  source_id: string;
  model_id: string;
};
type UsageBucket = {
  key: string;
  start_at: string; // RFC3339, inclusive, explicit server-local UTC offset
  end_at: string;   // RFC3339, exclusive; current bucket ends at report time
  history_complete: boolean;
  rows: UsageBucketRow[];
};
type UsageReport = UsageSummary & {
  window_key: UsageWindowKey;
  granularity: 'hour' | 'day';
  from_at: string; // first bucket start
  to_at: string;   // report time
  buckets: UsageBucket[];
};
```

`buckets` is dense and chronological: 24, 7, 30, or 60 entries. Daily keys
are local `YYYY-MM-DD`; hourly keys are offset-aware instants, unambiguous
across DST. Hourly intervals are actual consecutive hours, with the final
interval partial. Daily intervals are server-local calendar days.
Hourly identity is UTC-aligned across persistence, queue coalescing, and report
projection, then displayed with the server-local offset. Fractional-offset time
zones can therefore display boundaries at `:30` or `:45`; a DST offset change
must never move a measured call between incompatible bucket grids.

Each bucket's rows are sparse, unique source/model pairs. `sources` supplies
their existing joined display identities. `totals`, `sources`, and `days`
aggregate these exact same rows, including for hourly reports. For hourly
reports `window_days` names the number of local calendar dates touched by the
reported buckets; it must not pretend that 24 hours means today's calendar day.
An hourly interval can intersect two daily storage owners; the report joins
both contributions into one source/model row. Its `days` rollup follows bucket
start dates, while daily reports retain the original local-calendar accounting.
Daily reports preserve the legacy aggregate's bounds and survivor set.

`history_complete` means the time granularity is available for that bucket;
it is independent of `token_reports`, which describes vendor reporting.
Legacy daily usage must never be allocated to an invented hour using its
last-metered timestamp. Preserve it in daily reports, report only measured
hourly counts, and mark affected hourly buckets incomplete. The UI renders
gaps/partial-history language rather than claiming zero historical usage.
A completely idle, fully observed bucket can display zero. A bucket containing
requests but no token reports displays an unavailable token value, not zero.

Only the ledger produces the temporal matrix; the service joins identities;
RPC and HTTP preserve the fields; the browser projects filters/groups over
this one matrix. This report remains isolated from admission and routing.
No credential or upstream request-body fields are added.

## Storage boundary and ownership

The existing bounded ledger remains the sole metering owner. Extend it with
bounded recent hour aggregates without discarding the released list-of-daily-
rows format. Prefer optional nested hour slices in the retained daily rows:
one atomic write captures each call in both granularities. Older files load
without fabricated hourly history. Daily totals remain authoritative for
daily reports and include pre-upgrade usage; hourly slices are never added to
those totals a second time. Retain only the hourly slices needed for the
24-hour view, with explicit capacity and corruption handling. Persist the
aggregate counters pruned outside that horizon so a partial oldest local day can
distinguish ordinary retention expiry from a missing in-horizon slice. The optional
internal `hourly_expired_before` field records their exclusive UTC-hour boundary;
if a clock rollback brings pruned evidence into range, hourly history is incomplete.
Nonzero expired counters without a valid boundary also retain uncertainty. Keep
UTC-recent rows through subsequent writes after host timezone changes, without
changing their original daily ownership or the daily reports' capacity policy.
Existing batching must not combine calls from different hours before the ledger sees
their temporal identity. It must also retain distinct local-day owners when a
UTC hour spans midnight; a fold is valid only within both temporal boundaries.

The backend lane owns Python, the JSON schema/API documentation, and Python
tests. The UI lane owns frontend code, localizations, frontend tests, and
UI-side fixtures. The orchestrator owns integration, this plan, public user
documentation, regression acceptance, and final PR/review/CI delivery.
Interface deviations must be agreed with the orchestrator before integration.

## Acceptance

1. Real metered calls survive restart and reconcile across daily/hourly totals,
   Source/model filters, token metrics, and series/table projections.
2. Released daily-only files keep their existing totals. Hourly history gaps
   are explicit; corrupt/future/DST boundaries do not fabricate known usage.
3. Cache is counted exactly once; missing vendor usage and actual zero differ.
4. Hover, click-away, explicit pin/unpin, Escape, touch, and keyboard controls
   work without moving or changing the time being pinned.
5. Focused backend and frontend tests, changed-file lint, and the production UI
   build pass. Exercise the boundary with real fixture data, including Unicode
   and same-labelled Sources/models. CI owns repository-wide gates.
6. User-facing browser checks run against hermetic fixtures or a task-owned
   local Incus environment; never modify the running personal Avibe instance.
7. Current-head Codex review, CI, and resolved-thread gates are required.
   Opening the implementation PR is authorized; merging/deploying is not.

## Temporal boundary review decision

The September 24 integration reproductions exposed repeated temporal-ownership
defects, so further lane-level patching was paused for an orchestrator review of
the entire write/coalesce/read path. The independent local review covered head
`124b5e4a5` and reported three findings. At this decision there is one formal
findings-bearing local review head and no implementation PR review head.
Companion docs PR #47 has one findings-bearing head (`1cbc090010`), solely for
publication ordering; its unresolved release-order thread remains a separate gate.

| Root cause | Scope decision | Consuming evidence |
| --- | --- | --- |
| Queue coalescing lost the local-day owner when UTC hours span midnight | A fold must share both the local calendar date and UTC hour, as well as source/model and report presence. Daily storage stays authoritative; hourly projection joins owners. | Queue two same-hour calls across Nepal midnight and compare persisted daily/hourly reports with separate writes. |
| Read projection bypassed time-dependent slice validity | Reuse the existing recent-slice policy at report time; future evidence makes the owning day's intersecting intervals incomplete, while ordinary expired slices do not. Never move rejected counts into another hour. | Future-hour and future-stamped current-hour fixtures, before and after a subsequent write, alongside ordinary-expiry coverage. |
| HTTP scalar lookup hid conflicting selector values | Validate every modern `window` value; allow identical valid repeats, reject conflicting or invalid repeats, and preserve legacy `days` parsing. | Actual HTTP selector tests in both parameter orders and through controller IPC. |

These changes preserve the wire and storage contracts and do not justify a new
ledger, migration, or time-zone subsystem. Tests passing on earlier heads did not
close these gaps: verification must exercise the queue and read/write boundaries,
not only direct ledger recording.

## September 25 delivery-loop diagnosis

The orchestrator re-read all 33 GitHub review threads, including resolved and
outdated threads, and their originating review commits. Nine heads have findings:
`03a839f423` (5), `ee6fe9c902` (4), `5f0a695f62` (5), `57653ae5ff` (3),
`acc11668c2` (3), `3e71916d82` (4), `53d8204785` (2), `8354e502ed` (3),
and `d6d6db179` (4). There is no clean pass. Comment positions can migrate to a
new head; these counts use the review's commit, not a comment's current position.
The repeated classes are temporal evidence/completeness, display identity
allocation, and detail interaction/accessibility. The circuit breaker therefore
requires a scope decision, not indefinite waiting for another review event.

The previous UTC read-path repair and its consuming timezone test were checked
directly. That test never recorded another call after changing timezone, leaving
the local-day write-retention gate untested. The expired aggregate reconciles
counts but lacks a temporal boundary. The missing-history projection assumes an
old local owner can locate uncertainty in the current zone. All three are failures
to carry UTC evidence through the entire read/write/report lifecycle.

Decision: keep the existing bounded daily ledger, wire contract, and approved
pending queue. Complete the UTC evidence policy at its existing owner:

- Thread `PRRT_kwDOPbFPYs6lwkc0`: preserve real, UTC-recent evidence across a
  subsequent write even when the old local owner day is outside today's grid.
  Keep the original daily ownership; do not invent a new daily allocation.
- Thread `PRRT_kwDOPbFPYs6lwkdB`: carry an exclusive UTC expiry boundary with
  pruned hourly counters. A horizon behind already-pruned evidence is incomplete;
  an old nonzero expired aggregate without a boundary is not proof of completeness.
- Thread `PRRT_kwDOPbFPYs6lwkdF`: a mismatched local owner cannot bound unknown
  hours after a timezone change. Include the authoritative latest UTC hour and
  conservatively retain uncertainty where its allocation cannot be recovered.
- Thread `PRRT_kwDOPbFPYs6lwkc8`: keyboard activation moves focus into the detail;
  dismissal returns focus without reopening it. Hover must not steal focus.
- CI: consume the already-merged SQLAlchemy constraint from master. Separately
  fix the ToastProvider-owned timer lifecycle exposed by the actual UI CI log;
  do not classify that uncaught exception as a dependency installation failure.

Verification must first fail on the pre-fix owner boundary, then cover timezone
changes plus subsequent writes/reopened ledgers, backward and ordinary forward
retention, partial/no local-day overlap, and keyboard activation/dismissal. Use
focused tests, a production UI build, real fixture browser/HTTP+IPC acceptance,
and current-head CI. Do not redesign storage, alter public docs, manually trigger
Codex, or merge/deploy/restart the live service. Preserve the existing durable
Watch and report the final gated head to the coordinating session.

The independent local review of `aac9d257b` reproduced a remaining capacity
interaction: an old-zone future owner could outrank a newer call at `max_rows=1`.
Membership and eviction must use compatible evidence. For write retention and
hourly capacity only, derive the recency calendar label from usable UTC evidence,
not the old owner label. Keep the persisted owner unchanged and retain the exact
existing day-first policy for daily report capacity. Ordinary same-zone ordering
is unchanged. Verify both timezone directions with and without hourly history,
both with room for the old row and with capacity for only the newest call.

Integrated browser acceptance exposed a second dismissal path: removing the
mobile tooltip reveals the SVG underneath a stationary pointer, generating
pointer-enter without pointer movement. Pointer-enter may show ordinary details
but must not clear an Escape dismissal; deliberate movement, focus, or activation
can reopen them. Focus restoration is limited to users still inside the chart,
so Escape does not reclaim focus after Tab has moved outside this non-modal
detail. The existing desktop/mobile dismissal case covers both stationary
closure and subsequent deliberate hover; the keyboard unit case also covers
Tab-away dismissal.

After subscription quota PR #2171 landed as `6af1066eb`, the coordinating
session requested a master integration before readiness. Preserve both reports'
API/schema registrations, fixtures, scenario IDs, four-tab navigation, and
independent lazy-read lifecycles; the usage consumer continues to send modern
window selectors. Scope analytics-specific stat styling to its own root because
quota reuses the legacy stat classes. The quota browser case first reproduced
the unintended type-scale override and now guards the original quota styling.

## Known by design

- This is metered gateway usage, not native subscription quota or monetary cost.
- Historical daily counts cannot be reverse-engineered into hourly data.
- The approved preview's illustrative sidebar/data are not production changes.
- Source/model labels come from current configuration; deleted identities keep
  the existing honest historical-identity rendering.
