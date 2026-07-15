# Tunnel Quality Monitoring and Route Recovery

## Summary

Avibe currently treats a live `cloudflared` process as a healthy remote-access
tunnel. That is necessary but insufficient: all four connector paths can remain
available while their Cloudflare edge route has become slow or lossy. In the
July 15 incident, the local UI answered in about 2 ms, but a connector that had
moved from Singapore to Los Angeles reported 172-418 ms QUIC RTT and remote
requests reached 0.9-3.0 seconds. A connector restart returned all four paths to
Singapore at 68-96 ms RTT.

This design adds one local aggregate, `TunnelQualitySnapshot`, and makes it the
single source for:

- the local Remote Access status UI;
- a new Remote Access group in Doctor;
- a one-minute quality update alongside the existing runtime heartbeat; and
- a guarded make-before-break route recovery loop.

The target behavior is autonomous but conservative. A healthy connector is
never rotated on a timer. Avibe starts a temporary second connector only after
sustained evidence of availability, latency, or packet-loss degradation. The
candidate is promoted only when its measured route is materially better.

Affected repositories:

- `avibe`: metrics collection, quality evaluation, Doctor, local UI, connector
  supervision, and runtime-status reporting.
- `avibe-bot-backend`: additive runtime-status contract, latest-snapshot
  persistence, and current health display.

The cross-repository payload contract is frozen in
`docs/plans/contracts/tunnel-quality-runtime-status-v1.schema.json`.

## Product Decisions

1. Tunnel RTT is a first-class health signal, but not a standalone truth.
   Availability, request errors, and packet loss remain co-equal inputs.
2. User-experience grading and automatic-recovery eligibility are separate.
   Absolute RTT says whether the connection feels fast; relative RTT versus the
   user's healthy baseline says whether a new route is likely to improve it.
3. Edge location is diagnostic only. Avibe never assumes that a city code is
   good or bad and never triggers recovery solely because a location changed.
4. The local runtime owns detection and recovery. avibe.bot displays the latest
   state but does not remotely command connector lifecycle changes.
5. Cloud reporting sends one bounded aggregate, not raw Prometheus samples,
   edge IPs, connector IDs, request URLs, or time-series telemetry.
6. Normal operation uses one connector. A second connector exists only during
   evaluation and drain.
7. V1 does not expose threshold tuning. It provides one manual "Optimize route"
   action and a release-level auto-recovery rollout gate. This avoids freezing
   network heuristics into user configuration before field evidence exists.

## Cloudflare Capability Boundary

Cloudflare supports multiple `cloudflared` processes for the same tunnel as
replicas. Each process creates four new connections, keeps the same tunnel UUID,
hostname, routes, TLS identity, and Avibe session cookies, and receives its own
connector ID. Replica overlap is Cloudflare's supported pattern for availability
and updates without downtime.

Cloudflare does not provide connector-level latency steering or a public API to
move one healthy connection to a named city. Replicas can receive traffic as
soon as they connect, and Cloudflare does not guarantee which replica is chosen.
Avibe's candidate evaluation and promotion policy is therefore local
orchestration built on the supported replica model, not a Cloudflare latency
load balancer.

Cloudflare's OS service installer permits one installed service per host. Avibe
does not install cloudflared as an OS service; it owns background child
processes. The candidate must use that existing process supervisor and must not
attempt a second `cloudflared service install`.

Official references:

- [Tunnel metrics](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/monitor-tunnels/metrics/)
- [Tunnel availability and replicas](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-availability/)
- [Deploy cloudflared replicas](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-availability/deploy-replicas/)
- [Tunnel run parameters](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/)

## Terminology and Aggregate Root

`TunnelSupervisor` is the only owner of connector processes and quality state.
It replaces the current single-PID assumption with an explicit aggregate:

```text
TunnelSupervisor
  active connector       required during normal operation
  candidate connector    optional, only while evaluating
  draining connector     optional, retained until old-process exit is verified
  quality snapshot       current active-connector health
  healthy baseline       rolling route baseline
  recovery episode       state, attempts, backoff, last result
```

Each connector record owns its PID, start time, metrics address, log paths,
binary signature, and token fingerprint. Secrets remain environment-only and
must never enter the connector state file, logs, Doctor output, or cloud status.

The existing primary PID/status shape remains readable for compatibility, but
all new lifecycle operations go through `TunnelSupervisor`.

## Metrics Collection

### Source

Avibe starts every managed connector with an explicit loopback-only metrics
address:

```text
cloudflared tunnel --metrics 127.0.0.1:<allocated-port> --no-autoupdate run
```

The selected port is persisted with that connector. Avibe must not guess that
the endpoint is always `20241`, because cloudflared can select `20242-20245` or
a random port. Active and candidate connectors must use distinct ports and log
files. On upgrade, a running managed connector without both a persisted metrics
address and the `--metrics` argument is restarted once to enter this model.

Use a Prometheus text-format parser rather than regular expressions. Scrape
timeouts are 500 ms and failures never block API or Doctor requests.

### Sampling

- cloudflared updates its metrics every 5 seconds by default.
- Avibe samples every 15 seconds.
- Rate metrics use a 60-second rolling window.
- The UI reads the latest local snapshot; it does not scrape cloudflared.
- Store minute aggregates for 24 hours in bounded local runtime state. Do not
  persist per-15-second samples indefinitely.

### Inputs

The active connector quality evaluator consumes:

- `cloudflared_tunnel_ha_connections`;
- `cloudflared_tunnel_server_locations`;
- `cloudflared_tunnel_request_errors` counter delta;
- `quic_client_smoothed_rtt` per connection;
- `quic_client_latest_rtt` per connection;
- `quic_client_lost_packets` timeout counter delta, requiring positive deltas
  on at least two connections in the same rate window;
- `quic_client_closed_connections` counter delta; and
- `/ready` as a startup/readiness gate.

QUIC RTT is the connector-to-Cloudflare-edge round trip, not browser end-to-end
latency. The deep Doctor and a suspected-degradation confirmation probe may also
measure the public `/health` URL, but public probe latency is not part of the
cloud contract in V1 because the probe originates from the same host.

For HTTP/2 connectors, QUIC RTT is absent. `rtt_ms` is `null`; availability,
request errors, and a bounded public health probe determine quality. Missing RTT
is `unknown`, not automatically degraded.

## Healthy Baseline

Fixed RTT thresholds are not sufficient across countries and ISPs. Avibe keeps
a local baseline so recovery responds to route regression rather than normal
geographic distance.

- The baseline is the p20 of healthy one-minute median-RTT samples from the last
  24 hours.
- Only samples with four HA connections and no error/loss trigger enter the
  baseline window.
- At least 15 healthy minute samples are required before the baseline is valid.
- A connector change does not erase the baseline.
- Thirty continuous healthy minutes end a degradation episode and reset its
  retry backoff.

The p20 intentionally represents a repeatedly achievable good route. A rolling
average would drift upward during a prolonged bad route and eventually hide the
regression.

## Quality State and High RTT

The shared evaluator returns both:

- operational `state`: `healthy`, `degraded`, `recovering`, or `unknown`; and
- absolute latency `grade`: `good`, `fair`, `poor`, `critical`, or `unknown`.

Status, Doctor, cloud reporting, and recovery all consume this result; no
surface may maintain separate thresholds.

### User-experience grade

The grade uses median RTT across the four active paths plus the worst path. It
does not use an average, because one very slow path can still receive requests
while an average hides it.

| Grade | Active-path median RTT | Active-path maximum RTT | User meaning |
| --- | --- | --- | --- |
| Good | `< 120 ms` | `< 250 ms` | Responsive interactive use |
| Fair | `< 200 ms` | `< 400 ms` | Usable, with noticeable delay |
| Poor | `< 350 ms` | `< 700 ms` | Slow; Doctor warns after persistence window |
| Critical | `>= 350 ms` | `>= 700 ms` | Severely delayed |
| Unknown | RTT missing/incomplete | RTT missing/incomplete | Availability may still be healthy |

Rows are evaluated in order and both median and maximum must satisfy a row;
otherwise evaluation falls through to the next grade. Therefore "high RTT" for
user experience starts when median RTT reaches 200 ms or any active path reaches
400 ms.

Poor or critical latency must persist for 12 consecutive 15-second samples
(three minutes) before operational state becomes `degraded`. This is the
three-minute rule; a single spike changes the displayed number but not health.

### Non-latency degradation triggers

| Signal | Threshold | Required duration |
| --- | --- | --- |
| No ready connections | `ha_connections == 0` | 15 seconds |
| Partial availability | `ha_connections < 4` | 60 seconds |
| Request errors | `>= 3/minute` | 2 consecutive windows |
| Timeout packet loss | `>= 10/minute` across at least 2 connections | 2 consecutive windows |
| Metrics unavailable | State becomes `unknown`; never rotate from this alone | 45 seconds |

Availability and request-error triggers do not wait for RTT.

### Automatic latency-recovery eligibility

Degraded display does not automatically mean repeated connector churn. A
latency candidate starts only when the route is both objectively slow and
materially worse than the user's known-good route:

| Baseline state | Candidate trigger, sustained for 3 minutes |
| --- | --- |
| No baseline yet | `median >= 250 ms` or `max >= 500 ms` |
| Baseline available | `median >= 350 ms`, or (`median >= 200 ms` and `median >= 2 x baseline`) |
| Baseline available | `max >= 700 ms`, or (`max >= 400 ms` and `max >= 3 x baseline`) |

The absolute critical clauses ensure a historically slow baseline cannot
disable recovery forever. The relative clauses prevent users whose normal
geography is 180-220 ms from starting a new connector every few minutes merely
because their normal route is not globally fast.

Example: with an 85 ms baseline, high recovery-eligible median RTT begins at
200 ms and high worst-path RTT begins at 400 ms. The July 15 connector had about
259 ms median and 418 ms maximum, so it would have triggered after three
minutes. With a 150 ms baseline, the corresponding relative median threshold is
300 ms, while the absolute critical threshold remains 350 ms.

### Recovery hysteresis

A degraded state returns to healthy only after five continuous minutes with:

- four HA connections;
- no request-error or packet-loss trigger; and
- median RTT below `max(160 ms, 1.5 x baseline)` when a baseline exists.

This prevents flapping around a threshold.

## Candidate Attempt Schedule

There is no periodic candidate while healthy.

When a degradation trigger matures, the first candidate starts immediately. If
the candidate fails to start or offers no material improvement while the active
route remains degraded, retry using episode-local backoff:

```text
attempt 1: immediately
attempt 2: 15 minutes later
attempt 3: 30 minutes later
attempt 4: 60 minutes later
attempt 5: 120 minutes later
later attempts: every 6 hours while continuously degraded
```

Additional rules:

- Never run more than one candidate.
- Never run more than two attempts in a rolling 30-minute window.
- A manual Optimize Route action may bypass the current cooldown once, but it
  cannot create a second concurrent candidate. Its promotion criteria follow
  the active degradation signal (availability, errors/loss, or latency), while
  `manual` remains only the cooldown-bypass reason.
- Thirty healthy minutes after the latest attempt reset the episode and attempt
  count.
- Service restart resumes persisted cooldown instead of immediately retrying.
- When the active connector has zero ready connections, availability restoration
  takes priority over cooldown; one emergency candidate attempt is allowed.

## Make-Before-Break Recovery

### Candidate evaluation

1. Start a second connector with the same tunnel token, origin, and remote
   configuration, but distinct PID, metrics port, and logs.
2. Wait up to 30 seconds for four ready connections.
3. Collect candidate samples for 45 seconds, requiring at least four valid samples.
4. Reject the candidate if it has fewer than four connections, any request
   errors, unstable metrics, or a worse maximum RTT than the active connector.
5. For a latency episode, promote only when candidate median RTT is both:
   - at least 25 percent lower; and
   - at least 30 ms lower.
6. For an availability episode where the active connector has fewer than four
   ready connections, promote a stable candidate after it reaches four ready
   connections.
7. For an error/loss episode, promote when the candidate strictly improves
   request errors or packet loss without worsening either signal. RTT
   improvement is not required for this trigger.

Edge location is recorded but is never a promotion criterion.

The candidate becomes a live Cloudflare replica as soon as it connects, so it
may receive new requests during evaluation. This is why candidates are started
only after sustained degradation and rejected quickly when unhealthy.

### Promotion and drain

1. Atomically mark the candidate as active in local connector state.
2. Emit `tunnel_recovery_started` and an immediate cloud status update.
3. Send `SIGTERM` to the old connector. Do not call the current generic
   eight-second `stop_pid` path.
4. Allow cloudflared's 30-second grace period to stop accepting new requests and
   drain in-progress requests. Wait up to 35 seconds before escalating.
5. Long-lived SSE/WebSocket clients may reconnect once when the grace period
   expires. The hostname, DNS, OIDC session, CSRF state, and local Avibe session
   secret do not change.
6. Emit `tunnel_recovered`, update the quality snapshot, and enter cooldown.

This sequence removes the current break-before-make window and the mismatch in
which Avibe escalates after 8 seconds while cloudflared defaults to a 30-second
grace period.

During the overlap, the expected cost is four additional outbound connections,
one extra metrics endpoint, and one extra process. The observed macOS connector
used about 46 MB RSS and negligible idle CPU, so a candidate should budget about
50 MB of temporary memory. It normally exists for less than two minutes.

### Candidate rejection

If the candidate is not better, gracefully stop only the candidate, retain the
active connector, record `no_improvement`, and schedule the next backoff. A
failed attempt must never make an already-available tunnel unavailable.

### Crash recovery

Connector state writes are atomic. On startup:

- validate every recorded PID by executable identity and token fingerprint;
- reconcile the candidate PID file even when a crash precedes its aggregate
  state write;
- discard dead/stale records without signalling unrelated processes;
- if active and candidate both remain, retain the active connector and stop the
  orphan candidate;
- if only the candidate remains and is ready, promote it;
- retry cleanup of a recorded draining connector without losing its PID when
  termination cannot be verified;
- reset persisted `evaluating` or `draining` recovery state to a failed
  cooldown when no matching candidate or draining connector survives startup;
  and
- never clear shared logs or state belonging to the surviving connector.

## Doctor Integration

Doctor gains a `Remote Access` group. It is read-only and never starts a
candidate as a side effect.

Doctor reads the latest bounded snapshot without blocking on a metrics scrape:

- `pass`: enabled, process identity valid, four ready connections, quality
  healthy; include protocol, RTT min/median/max, and edge locations.
- `warn`: quality degraded, recovering, unknown, metrics unavailable, or one to
  three ready connections. Include the next automatic attempt time when known.
- `fail`: managed remote access is enabled but the connector is absent, has zero
  ready connections, or cannot reach the local origin.
- disabled remote access is an informational pass, not a warning.

Representative structured Doctor item:

```json
{
  "status": "warn",
  "code": "remote_access.tunnel_latency_degraded",
  "message": "Remote access latency is degraded (median 259 ms, max 418 ms).",
  "action": "Automatic route optimization will retry in 12 minutes.",
  "details": {
    "grade": "poor",
    "protocol": "quic",
    "ha_connections": 4,
    "rtt_ms": { "min": 172, "median": 259, "max": 418 },
    "baseline_median_rtt_ms": 85,
    "edge_locations": ["lax01", "lax07", "lax10"]
  }
}
```

V1 deliberately does not add public `/health` probes to Doctor. Those probes
originate from the same machine and do not represent a remote user's full path;
they can be added later as a separately labelled diagnostic.

RTT degradation alone is a Doctor warning, not a failure, because traffic can
still flow. It becomes a failure only through the existing availability or
origin-reachability conditions.

All new Doctor messages require English and Chinese i18n entries in the Web UI.

## Local Web Receiving and Experience

`GET /remote-access/status` adds the current `tunnel_quality` object. Replace the
current untyped `any` response in the React API context with the shared V1 shape
and explicit legacy/unknown fallbacks.

The current Remote Access page fetches once on mount and then relies on a manual
Refresh button. Quality and recovery need live delivery:

- publish the complete aggregate as `remote_access.quality.changed` through the
  existing local SSE broker, including recovery changes;
- update the page in place without a full page reload;
- retain a 30-second visible-tab fallback poll and refresh immediately on focus;
- stop fallback polling while the page is hidden.

Do not add a second global realtime subsystem for this feature.

The existing page already has a three-column operational strip. Extend that
strip rather than adding nested cards:

| Operational state | Primary label | Supporting text | Action |
| --- | --- | --- | --- |
| Healthy + good | Connection healthy | `84 ms - 4/4 paths - checked 8s ago` | None |
| Healthy + fair | Connection normal | `168 ms - usable, slightly delayed` | Details |
| Degraded + poor/critical | Connection slow | `259 ms - automatic optimization in 12 min` | Optimize now |
| Recovering | Optimizing connection | `Current access stays available while a better route is checked` | No duplicate action |
| Unknown RTT | Connected | `Quality metrics are temporarily unavailable` | Run diagnostics |
| Unavailable | Remote access unavailable | Existing start/repair guidance | Start or repair |

Display median RTT as the primary number. Put min/max, baseline, protocol, edge
locations, recent errors, and a 24-hour minute-level sparkline behind a details
disclosure. Edge codes should be translated to a location label when known and
fall back to the raw code only in advanced details.

After a successful promotion, show one non-blocking message such as
`Connection improved from 259 ms to 84 ms`. Preserve the current page, form, and
navigation state across the expected SSE reconnect; do not redirect or reload
the application shell.

Avoid exposing connector IDs, edge IPs, tunnel tokens, or internal log paths.

## avibe.bot Runtime-Status Contract

The current endpoint remains additive and backward compatible:

```http
POST /api/v1/instances/{instance_id}/runtime-status
```

Avibe adds an optional `tunnel_quality` property whose exact schema is
`contracts/tunnel-quality-runtime-status-v1.schema.json`. Example:

```json
{
  "event": "heartbeat",
  "local_version": "3.0.6",
  "ui_healthy": true,
  "tunnel_running": true,
  "cloudflared_found": true,
  "tunnel_quality": {
    "schema_version": 1,
    "state": "healthy",
    "grade": "good",
    "sampled_at": "2026-07-15T03:22:00Z",
    "protocol": "quic",
    "connector_count": 1,
    "ha_connections": 4,
    "rtt_ms": { "min": 68, "median": 84, "max": 96 },
    "baseline_median_rtt_ms": 85,
    "edge_locations": ["sin06", "sin12", "sin20"],
    "window_seconds": 60,
    "request_errors_per_minute": 0,
    "packet_loss_per_minute": 0,
    "recovery": {
      "state": "idle",
      "last_attempt_at": "2026-07-15T03:21:07Z",
      "last_trigger": "latency",
      "last_result": "improved",
      "previous_median_rtt_ms": 259,
      "result_median_rtt_ms": 84,
      "next_attempt_at": null,
      "attempt_count_window": 1
    }
  }
}
```

Reporting cadence:

- retain the existing five-minute core heartbeat and add one bounded quality
  update per minute;
- send immediately on quality state transitions and recovery outcomes;
- never upload the 15-second sample stream.

All runtime-status sends go through one serialized, coalescing reporter. A
scheduled heartbeat must not race a newer recovery event and overwrite it with
an older snapshot.

The backend validates the nested V1 object, stores it as an additive JSONB
column on the existing latest-status row, and returns it in serialized instance
status. It does not create a time-series table in V1.

### avibe.bot receiving behavior

Runtime liveness must not depend on quality-schema compatibility. The endpoint
parses the required core heartbeat first, then handles `tunnel_quality`
independently:

- missing quality means an older Avibe and remains valid;
- a supported, valid V1 object is stored;
- malformed or oversized quality is discarded while the core heartbeat still
  returns `200` and updates last-seen state;
- an unsupported future `schema_version` is ignored, not rejected; and
- a V1 sample more than five minutes ahead of backend time is discarded as
  unreasonable clock skew;
- validation failures are rate-limited in logs/Sentry without echoing payloads.

The backend rejects an older quality sample when its `sampled_at` predates the
stored sample, while still accepting the core heartbeat. The comparison and
coalesce happen atomically in the database conflict update so overlapping
heartbeats cannot let an older or missing quality payload overwrite the newest
valid snapshot. This is defense in depth behind the sender's serialized
reporter.

Quality freshness is evaluated independently. A fresh heartbeat with a quality
sample older than 150 seconds displays `quality unknown`, not an old RTT as if it
were current.

### avibe.bot user experience

The current console refreshes runtime status every two minutes. Change it to a
30-second refresh while visible, refresh immediately on focus, and pause while
hidden. Keep the server component model in V1; Supabase Realtime or another push
channel is unnecessary for one current-status row.

The bot card keeps its existing status pill and footer hierarchy, but gives
quality a distinct user-facing state instead of collapsing everything into
`NEEDS ATTENTION`:

| Quality | Pill | Footer |
| --- | --- | --- |
| Good | Online | `Remote access online - 84 ms - reported just now` |
| Fair | Online | `Remote access online - 168 ms - responses may feel slightly delayed` |
| Poor/critical | Connection slow | `259 ms - local Avibe will optimize the connection automatically` |
| Recovering | Optimizing | `Remote access remains available while Avibe checks a better route` |
| Recently improved | Online | `Connection improved from 259 ms to 84 ms` |
| Unknown | Online | `Remote access online - connection quality unavailable` |

The primary card does not show `SIN`, `LAX`, QUIC, packet loss, or connector
terminology. Those values may appear in an owner-only details view later.

The avibe.bot card health precedence becomes:

1. runtime status stale/offline;
2. local UI unhealthy;
3. tunnel stopped;
4. origin mismatch;
5. tunnel quality degraded or recovering;
6. last runtime error;
7. online.

The user-facing degraded state says that the remote connection is slow and may
be optimizing. Raw infrastructure terminology remains in an advanced detail,
not the primary status label.

Deployment order is backend first, then Avibe. The backend change is additive;
old Avibe versions omit `tunnel_quality`, and a newer Avibe talking to an older
backend still receives `200` because the existing endpoint ignores unknown
properties.

## Security and Privacy

- Bind metrics endpoints to `127.0.0.1` only.
- Never report edge IP addresses, connector IDs, tunnel token fingerprints,
  request paths, host network addresses, or public probe response bodies.
- Do not enable debug cloudflared logging; it can include request headers.
- Keep the existing instance-secret authentication for runtime status.
- The control plane stores only the latest bounded aggregate.
- Candidate processes receive the tunnel token through their environment, never
  command-line arguments or state files.

## Rollout

V1 ships observation, manual optimization, and guarded automatic recovery as one
cohesive state machine. Automatic recovery is enabled for managed Vibe Cloud
tunnels, can be disabled release-wide with `AVIBE_TUNNEL_AUTO_RECOVERY=0`, and
does not expose per-user threshold knobs.

## Scenario Catalog

| ID | Scenario | Required evidence |
| --- | --- | --- |
| RA-TQ-001 | Metrics produce a bounded quality aggregate | parser/evaluator tests |
| RA-TQ-002 | High RTT persists for three minutes before recovery | deterministic threshold tests |
| RA-TQ-003 | Missing metrics become unknown without route churn | evaluator resilience test |
| RA-TQ-004 | Better candidate replaces active before drain | supervisor integration test |
| RA-TQ-005 | Non-improving candidate leaves active untouched | supervisor rollback test |
| RA-TQ-006 | Local Web remains usable on desktop and mobile | browser/runtime proof |
| RA-TQ-007 | Doctor and avibe.bot consume the V1 aggregate | payload and backend contract tests |
| RA-TQ-008 | Restart removes an orphan candidate when active survives | crash-recovery test |
| RA-TQ-009 | Restart promotes a ready candidate when active is gone | crash-recovery test |

## Implementation Boundaries

1. Freeze and validate the JSON Schema before either repository changes its
   runtime payload.
2. Add a cohesive metrics parser, snapshot type, baseline, and evaluator in
   Avibe. Status, Doctor, reporting, and recovery consume this shared layer.
3. Extend avibe.bot's Zod input, domain type, Drizzle schema/migration, stores,
   serializer, tests, and bilingual current-status UI.
4. Replace the single-connector lifecycle with `TunnelSupervisor`; preserve the
   old status/CLI surface and migrate existing PID state in place.
5. Add local UI and manual optimization, then complete local Incus scenarios
   before enabling automatic recovery.

No lane may independently rename fields, alter enum values, or reinterpret
`ha_connections` as a total across active and candidate connectors. Contract
changes must update the schema first.

## Acceptance Criteria

- Healthy operation never starts a second connector on a timer.
- The July 15 latency shape reaches degraded within three minutes and attempts
  one candidate immediately.
- Absolute grade reports poor at 200 ms median or 400 ms worst-path RTT, while
  automatic recovery additionally applies the documented baseline/cold-start
  thresholds.
- A better candidate is promoted without changing hostname, DNS, OAuth state, or
  the local Avibe session secret.
- During overlap, exactly two known connector processes exist and each owns
  separate metrics/log state.
- Promotion drains the old connector before escalation; no break-before-make
  tunnel outage occurs.
- Failed/no-improvement candidates leave the active connector untouched and
  follow the documented backoff.
- Doctor and local status use the same quality classification.
- avibe.bot receives current aggregate quality on heartbeat and transitions,
  stores only the latest snapshot, and remains compatible with old clients.
- Invalid or future quality data cannot make an otherwise valid runtime
  heartbeat stale or offline.
- Local Web receives quality transitions through the existing event stream with
  bounded polling fallback; avibe.bot reflects them within 30 seconds while the
  console is visible.
- No secret or sensitive request data appears in local snapshots, logs, Doctor,
  status APIs, or cloud payloads.
- RA-TQ-001 through RA-TQ-009 pass in their required evidence layers.
