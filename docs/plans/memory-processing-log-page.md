# Native Memory Processing Record (superseded plan)

Status: superseded by the native Processing Record cutover

This plan originally specified a durable, installation-wide Provider Call Log.
That observer, its routes, and its UI are no longer part of the product contract.

The current contract is intentionally smaller:

- Processing Record reads caller-authorized EverOS native memcells, runs, linked
  semantic files, profile state, and index state.
- Native evidence is retention-bounded and best-effort. Missing or malformed
  evidence is reported as `partial` or `unavailable`; accepted loss is not replayed.
- There is no durable per-call observer, cross-request correlation ledger, gap or
  anomaly ledger, installation-admin projection, or fallback to a legacy log.
- Clear may delete legacy call-log files while that storage still exists, but no
  reader or recorder may depend on them.

See `docs/MEMORY.md`, `docs/MEMORY_ZH.md`, and
`docs/plans/memory-best-effort-capture.md` for the active behavior.
