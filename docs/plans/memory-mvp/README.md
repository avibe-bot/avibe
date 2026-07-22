# Memory Design Review Versions

This directory contains the converged local MVP proposal for review.

## Start here

In plain terms, the MVP does four things:

1. Avibe keeps a copy of eligible user text in a small local queue without
   slowing down the normal chat reply.
2. A background process turns that text into a profile and searchable memories.
3. The owner reads those memories from the Memory page, `/memory`, or
   `vibe memory`; Avibe does not inject them into agent prompts automatically.
4. The owner can pause collection or delete the whole Memory-owned data root.

The design uses a few recurring terms:

| Term | Meaning in this design |
|---|---|
| capture | accept one eligible user message into Avibe's local Memory queue |
| worker | the background loop that sends queued text for processing |
| provider | the memory engine; EverOS is the candidate for the MVP |
| sidecar | the provider process started and supervised by Avibe |
| UDS | a local Unix socket; here it means the sidecar has no TCP listening port |
| tombstone | a queue record whose message text has been erased but whose digest remains for duplicate detection |

The product document explains what users can do and what happens to their data.
The technical document explains how to implement that contract. The POC document
defines what EverOS must prove before implementation begins.

The current candidate includes automatic user-text capture from Workbench and
bound, enabled administrator DMs; the local Memory page; `/memory` reads from
Workbench and those private IM conversations; and local `vibe memory` reads.
Group IM, non-administrator DM access, agent-facing Memory tools, write-capable
command/CLI operations, and automatic recall remain deferred.
"Private IM" in this version therefore means a one-to-one conversation with a
bound, enabled administrator; a single shared pool cannot safely absorb ordinary
member DMs without a separate consent and scope design.

Memory is positioned as a built-in Avibe capability, alongside Vaults rather
than as an App Library app or a general plugin. Its pinned packages and native
runtime are presented as one Avibe-managed `memory-runtime` entry under
`/admin/settings/dependencies`; provider and transitive package details stay
behind that managed dependency. Its artifact installation extends Avibe's
existing managed-runtime Module; EverOS process ownership remains private to
Memory rather than being part of the Dependencies Interface.

The original rev37 documents remain at their existing top-level paths under
`docs/plans/`. They are restored byte-for-byte from commit `5dbf01aa` so
reviewers can compare the 36-round design with the smaller proposal without
using Git history.

## Review order

1. `memory-plugin-product-research.md` - provider choice and decision gates
2. `everos-1.1.3-deep-dive.md` - source-level EverOS behavior, APIs,
   storage, models, benchmark evidence, and alternatives
3. `memory-poc-everos.md` - evidence required before production integration
4. `memory-plugin-everos-phase1.md` - user-visible MVP contract
5. `memory-plugin-everos-phase1-tech.md` - implementation boundary and tests

## Version map

| Version | Location | Purpose |
|---|---|---|
| Original rev37 | `docs/plans/memory-*.md` | Full pre-convergence design baseline |
| Converged MVP | `docs/plans/memory-mvp/` | Review candidate with deferred capabilities removed |

The MVP POC is still marked `not run`. Review should distinguish agreement on
the product/technical boundary from evidence that EverOS passes the provider
gate.
