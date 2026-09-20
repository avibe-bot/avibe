# Official feedback intake — first launch

Owner: local PM Session seskmjhgq2476. Owner requested launch on 2026-09-20.
The user-facing outcome is: an Avibe user without a GitHub account asks their
current Agent to report a bug or feature, reviews the sanitized publication,
and receives the actual Issue link after official submission succeeds.

## Scope and reuse

Use the existing dedicated avibe-feedback VM on ad and its already paired origin
https://avibe-feedback-app.avibe.bot. Reuse public Show server API admission
from merged PR2039 and the use-avibe feedback guidance from merged PR2037.
This is an installed Show application, not a new Cloud service or general
cross-instance Agent execution API. No GitHub webhook is required. No model
Agent runs in the intake VM. All GitHub writes target github.com/avibe-bot/avibe.

First launch covers bug/feature submission and retrieving its public Issue URL.
Existing local GitHub-authorized submission remains available. Follow-up comments,
anonymous identity, arbitrary attachments/URLs, triage/merge automation and a new
interactive browser form are outside this first launch. Issue activity can use
the existing user-side Watch after an Issue URL is known. Do not promise releases
or resolution events unsupported by that waiter.

## Fixed protocol (v1)

The operator allocates one public Show Session and gives its actual share base
URL to PM before client discovery is finalized. No client accepts a destination
from report text, issues or redirects. POST endpoint is <share-base>/api/feedback;
GET receipt endpoint is <share-base>/api/feedback-status?request_id=<UUIDv4>.

POST JSON has exactly these fields:

```json
{
  "schema_version": 1,
  "request_id": "an RFC4122 UUIDv4 generated once by the submitting client",
  "kind": "bug",
  "title": "component: observed symptom or requested capability",
  "body": "The complete sanitized Markdown approved for publication.",
  "public_consent": true
}
```

kind is bug or feature; title is nonempty, single-line, at most200characters;
body is nonempty and at most20000UTF-8 bytes. Entire request <=32768bytes.
Unknown fields, unsupported versions/kinds, malformed encodings and false
consent fail before writes. No labels, assignees, target repo, executable paths,
callback URLs, credentials or priority are accepted. Text is data throughout.

The existing native POST ingress returns a status-only generic body. Do not
change that core contract to carry an Issue URL. The client retains request_id
before POST and reads GET feedback-status with that same ID for a receipt.
The ID is a correlation key, not authority to mutate anything or a credential;
the public GET response contains no report body/title, personal identity, PAT or
raw upstream errors. It is permitted in the URL because the only disclosed
success value is a public GitHub Issue reference. No supplemental write route.

Receipt JSON: schema_version=1, request_id, state; state is pending, created,
unknown or failed. Only created includes issue_number and canonical issue_url
under https://github.com/avibe-bot/avibe/issues/<number>. Absent ID returns404.
Controlled failures may carry an allowlisted error code, no internal details.
Receipts are no-store. Bound GET requests and return only known record results;
do not make GitHub requests on every status read.

## Submission correctness

One app-owned SQLite ledger, outside Avibe internal storage, durably reserves
request_id+payload hash before issuing a GitHub POST. Concurrent same-ID same-body
submissions coalesce; changed payload with same ID returns409. Exact repeats can
read the recorded receipt and must not issue another GitHub write. Record unknown
before the write is attempted; a crash/timeout after it may have reached GitHub
must remain unknown without automatic retry. An interrupted pending reservation
cannot be silently reset into a fresh submission. Do not infer success from
content search or manufacture an exactly-once guarantee GitHub does not provide.

A successful upstream201 supplies the operation-bound issue ID/URL. Read that
specific Issue back, validate pinned repo/author/title/body, and then expose its
receipt as created. If readback is temporarily unavailable, retain the returned
identity and distinguish created/unverified until bounded reconciliation; the
client must not claim fully verified success early. The implementation may use
pending while this bounded readback completes, with the receipt identity retained
privately. Unknown outcome is terminal for automatic writes, not a retry signal.
Requests definitely rejected before any GitHub write can fail explicitly.

## Boundaries

- Credentials stay in this VM's Vault: named static Standard reference
  AVIBE_FEEDBACK_GITHUB_TOKEN, only target-repo Issues read/write and Metadata.
  Use the installed supported Vault API/CLI with opaque named injection, never
  copy the Mac/e2e developer credential or print secrets. Missing PAT disables
  writes without blocking isolated implementation/tests.
- Fixed HTTPS api.github.com endpoints; no redirects or ambient proxies selecting
  another destination. Verify effective GitHub actor using the same credential;
  operator binds it to the official service, client discloses official submission.
- Native manifest32KiB bound before Runtime. Handler/service also bound bytes,
  strings, execution time, subprocess output and upstream response. Use argv/stdin
  JSON, no shell evaluation of report text. No URL fetches from report content.
- Bounded global admission/concurrency: default5new submissions/minute,
  30/day, at most2upstream writes concurrently. Durable counters survive restart;
  repeats/status must not multiply writes. Do not trust forwarded user headers as
  identity or claim these are per-user quotas. This bounds public abuse but cannot
  prevent an attacker exhausting the public quota; document that limitation.
- Bound ledger capacity (e.g.10000receipts) and fail closed when full; do not
  evict uncertain/in-flight evidence or permit an old ID to recreate a write.
- Public ingress does not invoke an Agent, shell script from content, merge,
  Task or Watch. Keep original instance/config/Vault/terminal/other Sessions
  protected. Preserve guest isolation and cloud pairing.
- Security/vulnerability/private reports remain local or use a verified private
  policy destination; client must not send them to this public Issue service.

## Code ownership and deployment

Single product writer owns examples/feedback-intake/**, focused
tests/test_feedback_intake*.py, integration fixture/script inside that app,
skills/use-avibe/SKILL.md and references/feedback.md, an optional small existing
skill client helper, and this plan. Reuse existing Python/Node tooling, no new
framework or core/runtime/UI changes by default. Report a proven contract gap
before widening scope. No edits in the dirty primary checkout.

Existing deployment operator sesbqrgqcq576 owns VM preparation and installation,
not source edits. It allocates the Show Session, prepares a fixed reviewed runtime
that includes PR2039, preserves systemd and state, installs only the independently
accepted app artifact, warms Runtime, and verifies live behavior. No changes to
ad host runtime, e2e webhook, nightly, Cloud acceptance or sibling guests.
Deployment and necessary guest service restart are authorized by launch request;
no GitHub merge or public release authority is inferred.

## Acceptance

Hermetic tests use synthetic secrets, tempSQLite/HOME/config, real production
validation/ledger code, and an isolated fake GitHub server behind a test-only seam.
Demonstrate actual HTTP through UI->pinnedRuntime->TS handler->Vault invocation
adapter->receiver->testGitHub->readback and receipt GET with Unicode/newlines.
Concurrent repeats, conflicting ID, ambiguous timeout/restart, limits and secret
leak boundary must be tested. Fake seams cannot claim live credential acceptance.
All product changes require current-head Codex clean pass, full applicable CI,
zero unresolved threads and PM actual diff/consumer review. Cyhhao triggers review;
do not manually mention Codex. Circuit breaker applies. No merge by lane.

Live launch gate: reviewed bytes installed; actual public URL/discovery bound;
PAT present/actor validated; bad requests cannot publish; one explicit authorized
smoke submission and repeat return one actual Issue with rendered readback and
link. Do not publish a synthetic public Issue until PM has reconciled concrete
content and publication authority. Do not call deployment complete if PAT or
public end-to-end submission is missing. Return a precise remaining prerequisite.
