# Local ShowAccess Settings Lane

## Background

Issue #1498 separates per-page guest membership from Instance authorization.
Local Avibe owns each page's access mode, stable `/p` binding, exact-email list,
and revision. Avibe Backend must not store or decide that membership.

This implementation consumes the contract in PR #1496 at head
`ff3ee55e389decc48496d03925b5c77328d5fecb`. The contract is not copied into
this branch. This PR requires #1496 to merge first.

## Goal

Deliver the local settings aggregate and its owner-facing UI:

- persist `private | limited | public`, stable share ID, revision, and normalized
  exact emails in local SQLite;
- apply mode, share ID, and the complete email set atomically with revision CAS;
- serialize settings writes in the controller and cross the UI-process boundary
  over the verified internal socket;
- expose identity-checked, non-cacheable settings read/apply HTTP endpoints;
- present one sharing control for Private, Limited, and Fully public while
  keeping operational availability independent;
- directly retire the old hosted-email CRUD UI/client and the old visibility,
  rotate-link, and share-ID Web mutation surfaces.

## Design

`ShowPageStore.apply_access` is the transactional aggregate writer. Canonical
changes advance the revision once; canonical no-ops do not. A stale revision or
share-ID collision returns the current snapshot without a partial email change.

The UI server authorizes the route page before IPC. The controller owns write
serialization and performs SQLite work outside its event loop. Route page ID,
HTTP request page ID, IPC request page ID, controller result page ID, and HTTP
result page ID must all match before any exact-email data is returned.

The sharing UI submits one Apply request. A CAS conflict reloads the latest
snapshot. A share-ID collision keeps the user's draft. Narrowing access asks for
confirmation. Availability remains editable independently of the audience and
does not change the access revision.

## Lane Boundary

This lane does not implement Limited `/p` identity login or admission, remove
the legacy `show_page_email` Instance authorization source, or add shared Runtime
containment. Those are separate #1498 implementation lanes consuming #1496's
identity and Runtime contracts. Until those lanes land, this PR must not claim
end-to-end Limited guest access.

## Verification

- [x] Store invariants, email canonicalization, CAS, no-op, collision, and rollback
- [x] Migration upgrade/downgrade and released local page-shape preservation
- [x] Controller settings read/apply, malformed request, identity, and serialization
- [x] Internal client round trip, unavailable socket, and timeout behavior
- [x] HTTP authorization, identity equality, cache policy, and retryable failures
- [x] Three-mode UI, Limited-only fields, single Apply, CAS reload, and collision draft
- [x] Deleted-component, client, route, i18n, and hosted-email residue guard
- [x] Full focused backend and frontend suites
- [x] Production UI build
- [x] Desktop and mobile browser acceptance

## Review-loop scope decision

Three findings-bearing review heads exposed distinct root-cause classes: route
selection and hidden draft state, incomplete Limited-mode product projection and
stale async UI continuations, then an ambiguous IPC deadline around serialized
writes. The third-head audit traced the complete settings Apply path.

The controller's SQLite write runs in a worker thread and cannot be cancelled
reliably after the IPC request is accepted. A finite client read deadline could
therefore report failure while the write later commits. The smallest complete,
contract-preserving fix is operation-aware transport timing: settings reads keep
a finite deadline, while Apply bounds connection establishment only and waits for
the controller's definitive CAS result. Controller serialization and aggregate
semantics remain unchanged.
