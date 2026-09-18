# Public Show server API admission

Status: implementation contract approved by PM seskmjhgq2476 on2026-09-18; not deployed. Implementation owner: backend lane sesa4968u458k (analyst sesvfjhjkzzjy completed read-only research). Existing E2E consumer owner: reg/seszbebbz6fwq. Owner requests GitHub push to the existing E2E instance. This contract describes the minimum product capability needed to reuse its existing signed receiver.

## Outcome
An explicitly registered public Show POST endpoint can authenticate an ordinary server-to-server request without requiring browser cookies or Origin. All unrelated routes retain their existing protection. A separate webhook-only Show workspace uses this capability; the existing PR report workspace stays private. This page is an ingress artifact, not a second executor or scheduler.

## Frozen declaration
A workspace-local optional `.show-api.json` file defines only exact static POST handlers in this first version:

```json
{
  "schema_version": 1,
  "server_to_server": [
    {
      "path": "api/github-webhook",
      "method": "POST",
      "auth": "handler",
      "max_body_bytes": 1048576,
      "forward_headers": [
        "x-hub-signature-256",
        "x-github-event",
        "x-github-delivery"
      ]
    }
  ]
}
```

The shape above is new product behavior, not existing configuration. `auth: handler` explicitly assigns sender authentication and business authorization to trusted server code. Core neither owns a signing secret nor asserts that arbitrary application code authenticates correctly. Publishing author-controlled executable code already implies trusting that author; browsers and webhook payloads do not become authors.

The registered path is relative to public `/p/{actual_current_share_id}/`, fixed before any request, and maps to one concrete module (`api/github-webhook.ts` for this example). No wildcard, dynamic parameter, index fallback, URL query target selection, or private `/show/` admission. `POST` is the only supported method. Invalid/unsupported optional declarations disable this admission without failing startup. Missing declarations preserve released behavior. Removal, share revocation or non-public visibility revoke future admission using the existing Show lifecycle. Never publish the private PM report workspace for this task.

## Boundary invariants
- Preserve host/proxy validation and normal resource lifecycle. A single registration resolver binds current share/page/visibility, exact canonical route/method and real workspace-confined module. URI aliases or traversal cannot acquire admission, including encoded separators/double encoding, symlinks outside workspace, and fallback-only handlers. Do not execute JS while deciding exemption.
- The same resolved registration gates both the Origin/CSRF exception and forwarding. No broad `/p/*/api/*` exemption. Browser cookies, Authorization, Origin/Referer, hop-by-hop and client-controlled internal headers grant no authority and do not reach the handler. Core supplies the existing SHARED Runtime envelope. Page owner/instance identity or annotation/bootstrap tokens must not leak.
- Bounds are applied before any generic JSON/body materialization: native async dispatch with parse_json=False, original-byte streaming count, bounded read/total time, early declared-length rejection plus actual bytes counted independently. Unsupported Content-Encoding and ambiguous duplicated security/metadata headers are controlled failures. First version hard upper payload cap is1MiB, declared route limit may be lower. Manifest reads, route count, metadata names/values and responses are bounded. Reuse established constants where suitable; record chosen limits/tests in the implementation.
- Default Show header forwarding remains unchanged. Only the selected registration extends a constrained metadata header set; no credential/hop/internal header may be enabled by spelling/case. HMAC covers original body bytes, not decoded JSON or delivery/event headers. Bytes cross UI->Runtime->handler unchanged, including whitespace/nonASCII. No payload-derived command/Agent/session/repository/destination.
- Failure is secret-free generic no-store JSON. No redirects to login on registered ingress, payload/error reflection, or anonymous-triggered installation/recovery. Existing lifecycle may prewarm Runtime through authorized operations; unavailable ingress Runtime fails closed. Decide against inspected manager behavior, not the name of an automatic flag. Reuse existing read-only availability interface; if a narrow core/show_runtime.py extension is needed, report before expanding scope.
- Existing browser/private Show, guest/limited access, write-token and WebSocket behavior remains unchanged. Hermetic tests prove a representative attempted write cannot touch actual HOME/config/Vault/runtime/network state.

## Consumer contract (remote owner; not core product implementation)
The existing TS handler loads dedicated Vault PR_MONITOR_WEBHOOK_SECRET through Vault run; verifies original body HMAC in receiver.py before payload business parsing/inbox effects; persists sanitized facts transactionally before202. Signature does NOT cover X-GitHub-Event/Delivery. Validate immutable repository id and name, event/action/nested types; bound signed-replay amplification through digest coalescing as well as UUID collision checks. No payload instructions or merge authority.

Same external inbox + sole remotePM seszbebbz6fwq. One managed forever Harness drain Watch owns wake delivery; current dispatch.py cannot both submit `vibe agent run` and return0 to a Watch (double submission). Its batching/recovery must be adapted to Watch signal/receipt semantics:0 only for a new reportable batch, pending work retained across ambiguous receipt,75/64 marker for no event, no repeated busy/empty wakes. Consumer receipt/replay tests owned remotely. Existing Taskd0865690e8b4 remains every10min until actual push is verified; same Task becomes reconciliation later. No auto merge solely as connectivitytest, no new merge executor, Cloud acceptance unlock or nightly repairs.

## Accepted product scope
- `vibe/ui_server.py`
- new `core/show_api.py`
- `core/show_runtime.py`: PM-approved extension on 2026-09-18 to the existing request and transport options only; see implementation mechanics below
- `core/show_pages.py` only where required to reuse page/workspace lifecycle
- `storage/db.py`: PM-approved shared factory `read_only=False` option on 2026-09-18; no migration/importer/lock changes
- `tests/test_ui_show_pages.py`, `tests/test_ui_server_mutation_protection.py`, new `tests/test_show_api.py`, new `tests/test_show_api_integration.py`
- `tests/test_show_runtime_protocol.py`: PM-approved strict client factory adaptation to assert the local transport's `trust_env=False`, preserving existing timeout assertions
- `scripts/check_show_router.mjs` only for reusing its pinned real Runtime integration path
- `skills/use-show-pages/SKILL.md`, optional new `skills/use-show-pages/references/server-api.md` if it keeps entry concise
- this `docs/plans/show-server-api-ingress.md`

No V2/DB migration, UI/frontend, Cloud backend, Runtime protocol/repo or broad ui_compat expansion. No new dependencies silently. Report a genuine interface gap to PM before exceeding file scope. Product lane does not edit remote ops or local user runtime. PR2037 use-avibe guidance files are a no-touch zone.

## Acceptance
1. Focused invariant tests cover registration/lifecycle/canonical-path/actualmodule and default protected admission, header identity isolation, raw bytes and prebuffer size/time bounds, failclosed unavailable runtime, revocation, and sanitized errors. Extend existing tests; no new test framework or prose substring tests.
2. A real isolated HTTP chain UI public admission -> explicitly pinned real Show Runtime -> fixture signed handler -> temp inbox is required. Send nonASCII/whitespace original bytes, verify committed digest and one row for accepted request; tampering rejects without row. Entire path uses synthetic secret and test-owned config/home/state. No live ad/Mac service probe/write/restart. Existing real Runtime fixture/script may be reused; record exact SHA/artifact.
3. Focused existing private/public Show+mutation regressions, Ruff changedPython and relevant packaging/skill parse checks, then full expected CI+exact-head Codex review+zero unresolved. No UI build unless actual frontend source changes; ordinary packaging may build artifacts independently later.
4. Deployment later verifies real GitHub ping/redelivery and PR/CI event through public edge to inbox and one durable Harness receipt; mock acceptance or public ping alone is not completion. Cloudflare1010 seen from a Mac client is an uncertainty, not proof GitHub fails or succeeds.

## Delivery and activation
Implement on feat/show-server-api based at4e0214fcd846; open a real non-draftPR, let cyhhao trigger Codex, keep durable combinedWatch and PMgate, no merge. This contract commit is part of that PR, no stacked specPR. Product files belong to one lane.

Prepare a pinned installable artifact and rollback/state-preservation/health plan after gates; do not activate ad or change Cloud edge/GitHub hooks/Vault yet. The owner has authorized obtaining working e2e push; any request for remaining authority must identify a concrete necessary action against actual policies, after the result is reviewable. Do not demand approval simply because analysis prose said "separate". A live signing secret and admin-controlled hook registration remain concrete commissioning prerequisites.

## Implementation mechanics and evidence

- A native POST adapter dispatches with `parse_json=False`. The resolver runs
  after existing host/proxy and role hooks and supplies the registration used by
  Origin exemption and forwarding. The same resolver rechecks after upload, so
  revocation or declaration changes during a slow body prevent dispatch. Browser
  POSTs without registration keep legacy JSON parsing and normal protection.
- The resolver reads at most 16 KiB of regular manifest data, accepts at most 16
  unique routes, and requires a concrete confined `api/<path>.ts`. Paths use
  ASCII alphanumeric/underscore/hyphen segments and have a 256-character limit.
  Raw URI aliases and queries do not acquire admission. Manifest parsing rejects
  duplicate keys; invalid optional declarations disable admission.
- Body limits are 1 MiB hard maximum, 10 seconds for upload and 30 seconds for
  the complete admission and registered operation, including the first storage
  lookup, earlier request hooks, capability negotiation and response streaming.
  A syntactic candidate never grants admission: the current declaration still
  controls the exception. Once an unregistered browser request passes its normal
  admission hooks, its established handler timeout is preserved. Datastore
  construction, lookup and close failures produce fixed 503/no-store receipts
  without exception details or browser cookies. Request cancellation prevents
  subsequent forwarding; it does not forcibly terminate a synchronous worker.
  Requests allow at most 64 header fields / 16 KiB in total,
  with 16 declared metadata names of at most 64 characters and values of at most
  2048 bytes. Names are case-insensitive `x-` metadata under a fixed exclusion
  policy for credentials, hop/proxy/internal protocol and browser identity.
  Content type is forwarded; the regular Show header allowlist is unchanged.
- Request and response compression are rejected; declared body length is only
  an early bound and actual original bytes are independently counted. Duplicate
  request headers and connection-nominated metadata are controlled failures.
- PM independently approved `core/show_runtime.py` scope after inspecting the
  consuming call chain at `17d5258ac`: `request()` otherwise calls `ensure()` on
  a missing endpoint, `automatic=False` does not prohibit startup, and `status()`
  inspects installation rather than exposing a live endpoint. Existing
  `request(start_if_needed=True, max_response_bytes=None)` defaults retain all
  prior behavior. This ingress passes `False` and 64 KiB. Missing endpoint fails
  before capability probes or lifecycle operations. The existing transport owns
  streaming and process-fenced failure invalidation; oversized/encoded handler
  responses close their stream without invalidating a healthy Runtime.
- The first reviewed head `44666dc8594fb3e13e83bb35970e58a5c36279b5` had three
  findings in two root-cause classes: incomplete pre-handler public boundaries
  (deadline and datastore error containment), and environment-owned loopback
  egress. PM approved closing these classes together. All three Runtime HTTPX
  clients (transport, capabilities and health) now set `trust_env=False`.
  Artifact downloads and other external HTTP clients retain their own policy.
- The resolver uses `ShowPageStore(read_only=True)` through the existing shared
  SQLite factory's keyword-only `read_only=False` option. PM approved this narrow
  follow-through after inspecting the cold-store migration lock: `mode=ro` opens
  an existing database without directory/file creation, migration/bootstrap or
  journal-mode changes. It preserves hidden SQL parameters and a 5-second busy
  wait. Missing/unreadable/corrupt/schema-incomplete state fails with the fixed
  public 503 boundary; healthy absent declarations still fall back normally.
  Default stores and cached engines retain their existing lifecycle. SQLite WAL
  and SHM bookkeeping may still occur; this is not an immutable-file guarantee.
  Authorized startup owns initialization. Pre-existing UI config/startup hooks
  are not redesigned, and timeout does not claim to kill synchronous workers.
  Tests hold a real migration lock while resolving, prohibit both bootstrap
  entrypoints, delete a database between engine creation and connection, and
  verify URI encoding, unchanged journal mode and ordinary default Store writes.
- Bounded Runtime response materialization recalculates content length and
  removes transfer/encoding framing. The ingress exposes only a generic no-store
  receipt and accepted 2xx/4xx/5xx status, never handler output, redirect headers,
  cookies or exception detail. Redirects and response policy failures return
  502; unavailable Runtime returns 503. HTTP 204/205 receipts have empty bodies.
- Real integration uses Runtime `5a9a6a52f2ae03611d617a659bfd0c1c32389478`, the
  existing CI fixture pin, through actual HTTP UI -> Runtime -> TS handler ->
  Python signed receiver -> temporary SQLite inbox. It confirms exact non-ASCII,
  whitespace and newline digest, committed 202 receipt, tamper 401 with no new
  row, SHARED identity and revocation. HOME/XDG/config and representative child
  Vault-path writes are test-owned; credentials are synthetic. The existing
  `check_show_router.mjs` invokes this test using its already-built pinned
  Runtime, so mocks are not the only CI boundary evidence. The real chain also
  sets upper/lower HTTP/HTTPS/ALL proxy variables to a test-owned poison proxy
  with empty upper/lower NO_PROXY. Health, capabilities and exact signed payload
  delivery succeed; the proxy receives no traffic.
- Local validation after the first correction: 706 focused tests passed
  (78 new boundary, 30 Runtime protocol, 594 Show/mutation, four legacy guards),
  plus 69 selected Store/access tests and the pinned real integration;
  final PR/CI evidence is recorded in the delivery report. No service deployment,
  signing key commissioning, hook registration or external delivery is claimed.

## Source evidence
Analysis read Avibe4e0214fcd8460b68751affb3f00f04156d2970d1, Cloud backend9616a2f2e9b372cc762ef59decc972aaa3a89caa and Show Runtime5e31eda3536db3ea4de018fb253d0ed7d5c69a09. Actual installed e2e host is3.1.1.dev0+g4019b704c99a; these are distinct artifacts. Cloud uses hostname tunnel passthrough to UI5123, so no Cloud backend source change is indicated. Current Show API Origin guard and header allowlist block GitHub. Existing staged receiver tests:13passed synthetic/temp/mock, not live dispatch. Full dated recommendation remains a PM investigation artifact outside the branch.
