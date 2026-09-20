# Official Avibe feedback intake

This dedicated Show app accepts approved public bug/feature reports from the
user's current Agent. The fixed destination is `avibe-bot/avibe` (repository ID
`1035030370`) under `cs-agent-bot` (user ID `272811739`). Reports cannot override
that binding. No GitHub account is needed on the submitting client.

Allocated endpoint: https://avibe-feedback-app.avibe.bot/p/132vCvND49U/ on guest
`avibe-feedback`, Show Session `ses9u23mgw6dr`. PM observed the staged share private at access revision 2. Allocation does
not mean intake is active. The exact reviewed artifact, supported base package,
Runtime and live write/readback still require commissioning by PM/ops.

## Operator installation

Use the reviewed commit's `examples/feedback-intake/{api,lib,worker.py,.show-api.json}`
bytes as one artifact; preserve its hashes. Copy them into the existing workspace
`/home/avibe/.avibe/show/ses9u23mgw6dr`. Do not install the integration fixtures.
No public browser form or new page identity is needed.

The normal guest Avibe package must include native Show server POST admission
(base `b821f22f55b2230de176ede3a1183acbae81b528`). Use packaged Show Runtime
`5e31eda3536db3ea4de018fb253d0ed7d5c69a09`. Manifest SHA256:
`98725b13df13c206ba689609f5df198bd5cd9b83d3484b58142944a2775e7698`.
Linux x64 archive SHA256:
`ea1079eca7bf532192c72950e1c9cd6f183192fb3c30af6a87f7fa0a63305b6e`.

Bind these environment values in the operator-owned Runtime service context:

- `AVIBE_FEEDBACK_APP_ROOT=/home/avibe/.avibe/show/ses9u23mgw6dr`
- `AVIBE_FEEDBACK_DATABASE=/home/avibe/feedback-intake-state/ledger.sqlite`
- `AVIBE_FEEDBACK_PYTHON`: absolute Python executable in the normal installed
  Avibe environment, which includes its existing psutil dependency.
- `AVIBE_FEEDBACK_VAULT_BIN`: absolute supported `vibe` executable in that same
  installed environment. The default is `vibe` from the operator's PATH.

Create the dedicated ledger parent owned by the service user with mode `0700`.
The worker fails closed on missing/relative paths, symlinks, hardlinked database,
world-readable parent, or a path under AVIBE_HOME, legacy home or this app. Keep
all state outside every public Show root; do not link it into a workspace.
DB/WAL/SHM and lock files are private app state, separate from Avibe's internal DB.
Back up the ledger as a coherent SQLite database; never replace it with an empty
one during upgrade or remove uncertain records/slot files while workers run.

The dedicated static Standard Vault reference `AVIBE_FEEDBACK_GITHUB_TOKEN`
already exists; no new credential or Vault policy is required. The fixed
`vibe vault run --no-approval-wait --env AVIBE_FEEDBACK_GITHUB_TOKEN -- ...`
path performs opaque named injection. Production must unset every
`AVIBE_FEEDBACK_TEST_*` variable. Ambient tokens are removed before Vault;
HTTP uses only api.github.com without redirects or ambient proxies. No report
text enters shell commands or selects URLs. Protect service environment and app
files as trusted operator configuration.

Ops owns installation, warm-up, service lifecycle and activation. Product lane
performs none of those actions. Disable intake by removing its manifest entry or
making the page private; preserve the ledger. Re-enabling uses the same ledger.

## Protocol, bounds and recovery

POST `/api/feedback` has exactly schema_version=1, canonical lowercase UUIDv4
request_id, kind=`bug|feature`, title, body and public_consent=true. Title is one
line ≤200 characters, body ≤20000 UTF-8 bytes, complete request ≤32768 bytes.
The native Show POST returns only a generic status body. GET
`/api/feedback-status?request_id=<UUIDv4>` returns no-store state and, only after
verified readback, the canonical public Issue number/URL. IDs are public
correlation, not bearer secrets. Unknown IDs return 404; status never calls GitHub.

One transaction reserves ID, canonical payload digest, report and durable global
quota counters: 5 new reservations/minute, 30/day, 10000 total receipts. Changed
payload under an existing ID returns 409. Exact repeats never issue another write.
Two kernel file locks bound upstream workers across processes. Slot rejection
before a worker claims its row persists failed (known no-write), not unknown. Four handler
children per Runtime module and bounded body/output/time limit local work;
these are resource caps, not user identity or per-user rate limits. An anonymous
attacker can consume the public quota. No availability or exactly-once guarantee.

A pending reservation has a 20-second absolute cutoff. Workers arm their own
hard timer before authenticated calls; late delivery refuses to write. The
supervisor tracks actual descendants because avault starts another session.
Timeout/overflow kills tracked children; the worker deadline remains independent.
An upstream request already received by GitHub cannot be cancelled or proven lost.
The ledger records unknown atomically immediately before the first write and
never retries it. Pending expiry is durable failed; read-only identity binding
does not make the outcome uncertain.

A valid GitHub 201 identity is committed before readback. Only exact immutable
Issue ID, number, URL, repository, actor ID/login, title and body checks expose
created. Known GitHub rejection becomes failed; other outcomes remain unknown.
An operator can reconcile a retained 201 identity after its deadline with:

```sh
/path/to/installed/python /home/avibe/.avibe/show/ses9u23mgw6dr/worker.py reconcile UUID
```

Run with the same operator environment and supported Vault executable. This is
CLI-only and performs reads through named Vault run, at most three attempts per
receipt; it never creates another Issue. An unknown result without retained
identity requires human investigation; content search is not an operation receipt.
Do not reset a pending/unknown record to retry. Never evict evidence to free space.

## Client distribution and launch evidence

The helper is `skills/use-avibe/scripts/feedback_intake.py`, under the existing
Skill. Build a normal wheel (`npm ci && npm run build` in ui, prepare the supported
Runtime manifest, then `uv build --wheel`). The wheel force-includes the entire
Skill at `vibe/builtin_skills_source/use-avibe`. The ordinary supported installation
is `uv tool install --force /absolute/path/to/reviewed.whl`; normal startup binds
its content-addressed built-in snapshot. `vibe skill load -- use-avibe` in the
client must resolve the reviewed helper/reference. Do not edit caches, install a
new Skill identity, or infer availability from this source checkout. PM must
reconcile the final exact wheel/version/hash and installation authority.

The user approves the sanitized title/body, public repository and official actor
once. The helper commits the exact payload and UUID before POST and retains a
private attempt outbox under AVIBE_HOME (default ~/.avibe). `resume UUID` only
reads status in every phase. A directly observed POST429 is retained separately
from the next status query and sending intent. Only a later explicit identical
submit first obtaining a fresh minimal404 may attempt one same-ID/bytes delivery.
Unavailable/malformed/redirected status suppresses upload. Every observed server
receipt permanently revokes recovery; terminal evidence never regresses under
concurrent updates. An interrupted recovery is reported unknown, not rate
limited based on old history. This relies on the fixed receiver's unreset ledger
coalescing original and recovery deliveries into at most one GitHub write. An
initial unknown without observed429 history cannot be replayed. See the launch
plan's complete state table.
Saved terminal receipts survive polling outages. Security, vulnerability and
private reports stay off this public route. Existing direct user-authorized
GitHub behavior remains supported.

Before launch, independently review the exact head, all CI and review threads;
verify the installed artifact hashes, real Runtime→Vault identity context and
bad request rejection; then use one PM-authorized concrete public report to
prove Issues write permission, repeat coalescing and exact rendered readback.
No synthetic public Issue is authorized by this source. Local fake tests do not
prove live PAT write permission, Linux commissioning or installed client rollout.
See [integration instructions](integration/README.md).
