# Show Access Contract v1

This directory freezes the cross-repository contract for Unified Show Access and
Capability-Gated HMR. It is a contract-only layer: it does not implement storage,
authorization, routing, UI, migration execution, hosted endpoints, or Runtime
behavior.

The authority is `docs/plans/public-show-live-update.md` at Avibe commit
`38925dda50da6280ae63e323ba34a4ef601f5bc9`. JSON Schema files use Draft 2020-12.
`mirror-registry.json` names every future producer and consumer; a lane that changes
a field, endpoint, claim, header, or closed vocabulary must update this contract first.

## Contract files

| File | Frozen surface |
| --- | --- |
| `show-access.schema.json` | Durable non-PII `ShowAccess`, gate, and coordinator state. |
| `apply-mutation.schema.json` | The only owner-facing access mutation request/result. |
| `hosted-operation.schema.json` | Prepare, commit, status, current-grant, and acknowledgement messages. |
| `capability-matrix.json` | Closed `/p` and `/show` read/editor decisions. |
| `runtime-context.json` | Protocol 1 headers, negotiation, route/context invariants, and release gate. |
| `rollout.json` | Additive hosted rollout and the one-way legacy PUT enforcement boundary. |
| `fixtures/*.json` | State, Apply, hosted recovery, and migration examples. |
| `mirror-registry.json` | Exact producer, consumer, signature, and delivery registry. |
| `scenario-bindings.json` | Binding from all `SHOW-LIVE-001` through `SHOW-LIVE-038` to contract anchors. |

Every object is versioned with integer version `1`. Additive fields require a new
contract version unless their containing schema already marks them optional.
Fixture field `shared_route_admitted` always means admission of `/p`; canonical
`/show` access remains the independent resource decision in the capability matrix.

## API table

| Boundary | Method and route | Request | Result | Idempotency owner |
| --- | --- | --- | --- | --- |
| Local owner Apply | `POST /api/show-pages/{page_id}/access:apply` | `apply_request` | `apply_result` | Avibe, keyed by `page_id + mutation_id`; expected audience and grant revisions are compare-and-swap inputs. |
| Hosted prepare | `POST /api/v1/instances/{instance_id}/show-pages/{page_id}/grant-operations/prepare` | `prepare_request` | `prepare_result` | Backend, keyed by `page_id + mutation_id`; a canonical same set returns `no_change` and creates no operation. |
| Hosted commit | `POST .../grant-operations/{operation_id}/commit` | `commit_request` | `commit_result` | Backend, keyed by the bound page, mutation, and operation; retries return `already_committed` with the same revision and commitment. |
| Hosted operation status | `GET .../grant-operations/{operation_id}` | `operation_status_request` | `operation_status_result` | Backend retains terminal non-PII operation evidence. |
| Hosted current grant | `GET .../current-grant` | `current_grant_request` | `current_grant_result` | Authoritative page-scoped revision/commitment read; also carries the legacy-write enforcement marker. |
| Hosted acknowledgement | `POST .../grant-operations/{operation_id}/acknowledgement` | `acknowledgement_request` | `acknowledgement_result` | Backend; repeated acknowledgement is terminal and harmless. |
| Runtime negotiation | loopback `GET /capabilities` | none | `{protocol: 1, features: [...]}` | Runtime process identity; only explicit `show-context-key-v1` is positive evidence. |
| Runtime app graph | every loopback graph request | protocol and context headers | private or shared graph response | Typed `ShowRuntimeProtocolEnvelope`; individual callers do not construct headers. |

Released headerless clients retain the legacy singleton path. Protocol `1` requires
an explicit `private | shared` context, while any unknown protocol value is rejected;
`request_protocol_cases` freezes all three Runtime-side outcomes.

The abbreviated `...` in the table expands to
`/api/v1/instances/{instance_id}/show-pages/{page_id}`. Exact route files and symbols
are in `mirror-registry.json`.

## State invariants

- `availability` is orthogonal to `access_mode`; offline pages serve no route but may
  retain a future audience.
- Private state has no share binding. Limited and public state have exactly one share
  binding. A share ID is a locator, not a bearer credential.
- `grant_revision` and `grant_commitment` are both null or both present. An open
  limited gate requires both.
- A grant commitment is 32 backend-generated random bytes encoded as 64 lowercase
  hexadecimal characters. It is never derived from an email or client-held key.
- `closed_pending` exists only for limited access with a durable `grant_change`
  coordinator record. Every `/p` read and editor redirect is denied while closed.
- Leaving limited commits private/public locally with an open gate. Cleanup may stay
  pending, but cannot restore page-email authority or close an already-public route.
- `audience_revision` is device-local. `grant_revision` is backend-issued and scoped
  to one Show Page. The Instance authorization revision cannot substitute for either.
- Durable local state and coordinator records contain no exact email. Exact addresses
  occur only in an Apply/prepare operation input or an authenticated hosted result.

## Hosted terminal outcomes

Prepare has three closed outcomes:

| Outcome | Meaning |
| --- | --- |
| `no_change` | Canonical target equals the current set. Return the current revision/commitment; create no operation and do not close an open gate. |
| `prepared` | A fresh 256-bit target commitment and opaque operation ID exist for at most 24 non-renewable hours. Grant authority is unchanged. |
| `revision_conflict` | Expected page grant revision is stale. The authenticated current grant is returned for reconciliation. |

Commit is `committed | already_committed | expired_uncommitted |
revision_conflict`. Operation status is `prepared | committed |
expired_uncommitted | result_expired`.
`expired_uncommitted` is positive proof that the operation cannot later commit and
includes the authoritative source/current grant proof. `result_expired` says the
exact terminal copy is gone; the client compares the retained opaque target
commitment and source revision with `current_grant_result`. Ambiguity never opens a
limited gate. An already-limited audience may reopen after proven uncommitted expiry
only when current revision/commitment still equal its recorded source. Entry from
private/public remains limited and closed until a new owner Apply.

Acknowledgement is `acknowledged | already_acknowledged | operation_expired` and
allows the backend to delete the prepared exact-email copy. Terminal evidence keeps
only non-PII identifiers, revisions, and opaque commitments.

## Legacy PUT rollout

The released backend route
`PUT /api/v1/instances/{instance_id}/show-pages/{page_id}/authorized-emails` cannot
coexist indefinitely with enforced prepare/commit because it has no local admission
gate and would bypass the coordinator.

`rollout.json` therefore defines a one-way sequence:

1. `additive`: new endpoints exist, released clients may still use legacy PUT, and
   Avibe must not activate the new access schema.
2. `enforced`: authenticated `current_grant_result.legacy_write_policy` is
   `disabled`; legacy PUT returns HTTP 409 `show_grant_protocol_required`; only
   prepare/commit can mutate grants. Avibe may activate the new schema only here.
3. `retired`: legacy PUT returns HTTP 410 and remains unable to mutate authority.

The marker is authenticated hosted output, not a client flag. Once enforcement is
advertised it never moves backward. Old clients fail visibly after enforcement; they
cannot write around the gate.

## Capability boundary

`capability-matrix.json` is a compressed closed matrix. Tests expand all
`2 surfaces x 2 availability values x 3 modes x 2 admission-gate states x 4 principals x 3 Runtime outcomes`
and require exactly one matching rule per combination.

- `/p` is shared and has no HMR for every viewer. A supported Runtime may redirect an
  authorized editor's trusted top-level navigation before returning page bytes.
- `closed_pending` denies every `/p` read and editor redirect. Canonical `/show`
  remains independently governed by resource authority and never consumes the
  page-email gate.
- `/show` is private. Ordinary independently authorized resource viewers may read its
  complete module graph, but only `owner_editor` has `ShowEditorCapability`,
  annotations, and HMR.
- Matrix principals are effective server-owned capabilities: an independently proven
  editor is `owner_editor` even when the same email is listed; `page_email_viewer`
  means page-email authority only.
- `page_email_viewer` may satisfy only an active limited `/p` read at the current page
  grant revision. The claim never authorizes `/show`, resource APIs, annotations,
  HMR, settings, Workbench, or Agents.
- Public authorization failure stays shared and readable. Limited authorization
  failure is login/denial with no page bytes. Offline always denies.

The hosted signature covers `vibe_show_page_id`,
`vibe_show_page_grant_revision`, and
`vibe_instance_access_source=show_page_email` together. Browser input supplies none
of them.

## Runtime release gate

The currently reviewed Runtime baseline is pinned to PR 59's exact reviewed head,
`ee3b0b490ad8b4afafb59cf37e2d57a20325208a` (merged as
`c2d5acc3a021cf62161919214a63a51ff313351b`). That baseline is not a keyed-context
implementation and `runtime-context.json` explicitly sets
`feature_advertisement_allowed=false`.

The Runtime must not advertise `show-context-key-v1` until all of these are true in
one reviewed Runtime head:

1. Delivery item 6 is implemented: total protocol/context propagation and separate
   private/shared context ownership.
2. Delivery item 9 is implemented: immutable opaque shared graphs, confinement,
   provenance, header filtering, and bounded admission.
3. The bundled Runtime smoke test passes on that exact head, recorded separately as
   `smoke_tested_runtime_sha`.
4. This contract is updated to pin that exact 40-character SHA in both
   `reviewed_runtime_sha` and `smoke_tested_runtime_sha`, then sets the three evidence
   fields to `implemented`, `implemented`, and `passed` before changing
   `feature_advertisement_allowed` to true.

Schema validation rejects the current baseline and any missing review or smoke SHA
when advertisement is true. Contract tests additionally require the reviewed and
smoke-tested SHAs to be identical. Avibe still accepts Runtime support only from the
wire capability response; a package version or successful app request is not proof.
