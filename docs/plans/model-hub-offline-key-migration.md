# Native API-key migration without upstream validation

## Owner decision (2026-10-14)

Migration transfers saved configuration and credentials; it is not an upstream
connection test. The owner explicitly replaced the earlier partial-skip proposal:
import all locally valid selected API-key entries without authenticating them,
probing their protocols, or fetching their model lists during migration. An
expired key, offline endpoint, or unavailable model list must not prevent or slow
the batch. Normal Gateway use reports provider failures after migration.

The earlier partial-skip proposal is superseded; no skipped-result API, report UI,
or receipt schema is added. The public response remains `{applied, sources, added_to}`
and the existing transaction/replay format stays unchanged.

## Smallest complete change

- In shared migration preparation, remove static-key observation for both new
  Sources and matching existing Sources. Native parsing already supplies the
  protocol and target; do not re-detect or rewrite the protocol over the network.
- Provision selected keys into the existing custody adapter and construct Sources
  from saved metadata. Preserve declared manual models and known configuration;
  never fabricate upstream-discovered models or claim successful authentication.
- Newly imported static Sources use the existing `verification_pending` marker
  and neutral Saved presentation. A reused Source retains its existing identity,
  credential ref, model inventory and current health/evidence; migration does not
  newly verify it or reset an existing failure.
- Preserve useful model configuration and selections without making catalog
  download another migration admission gate. No per-provider upstream discovery
  in the static-key migration call path. Ordinary background discovery and later
  explicit tests/requests remain separate owners.
- Keep Avibe-owned named Agent selections runnable when entering Gateway mode.
  If a migrated static Source has no inventory, carry the selection through its
  supported passthrough path. OpenCode's native provider prefix must match a
  Source actually imported or reused in this batch. Preserve removed-model
  intent and retired Source/model pairs; do not fabricate discovered entries.
- Keep local format/URL/transport representability checks, stale-consent and file
  guards, credential access checks, backend retirement, copy-vs-cleanup consent,
  atomic write-ahead custody and recovery exactly as required. Do not skip or
  erase keys because they might be invalid upstream.
- OAuth is a separate grant-ownership transfer: its existing activation, refresh
  rejection, and forward-only recovery rules are unchanged by this static-key
  decision. Engine installation/startup readiness is not API-key authentication.

## Copy and tests

Migration copy says keys are saved, not tested; failures from local config/custody
checks must not tell users to validate a key online. Reuse existing Saved status
and its hints rather than adding a new partial-migration UI.

Primary regression at shared migration owner: make every static observation /
model-discovery function raise if called; import mixed good-looking, rejected and
unreachable fixture keys (same backend and across backends), preserve protocol,
manual models and configured transport, check neutral pending marker, copy/cleanup,
identity reuse and receipt replay. The pre-fix code must fail because it invokes
observation. Old tests demanding proof, protocol re-detection or auth-failure
rollback must be updated to the new owner-approved contract, not simply deleted.
Existing transport/permission/concurrency/OAuth safety tests must continue passing.

Independent integration: real HTTP/CSRF/controller IPC + a mock upstream that
rejects all requests; assert all keys import and no credential-bearing observation
or discovery request was made during migration. Verify replay and native copy/
cleanup in the disposable local Incus worktree, never on a live tenant.

## Validation and delivery

- MH-MIG-015 protects local transfer, saved protocols, neutral pending markers,
  identity reuse, native copy/cleanup, and identical replay without any upstream
  call. Its pre-fix failure is an attempted observation.
- MH-SWITCH-001 keeps native model selections usable without discovery; a removed
  model stays removed and an unrelated provider selection stays unconfigured.
- The real HTTP/IPC suite passed all seven migration cases against a rejecting
  fixture upstream inside an isolated local Incus worktree. Additional persisted
  shell/native-input checks retain their file and credential-custody boundaries.
- Migration dialog tests and the UI production build passed. Related existing
  proof tests are updated to assert no observation, while keeping local consent,
  canonical-value, transport, rollback, and OAuth recovery checks.
- Delivery requires exact-head review and CI. Public documentation is coordinated
  with the release; no deployment or service restart is implied by this change.
