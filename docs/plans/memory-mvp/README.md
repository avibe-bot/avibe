# Memory Design Review Versions

This directory contains the converged local MVP proposal for review.

The original rev37 documents remain at their existing top-level paths under
`docs/plans/`. They are restored byte-for-byte from commit `5dbf01aa` so
reviewers can compare the 36-round design with the smaller proposal without
using Git history.

## Review order

1. `memory-plugin-product-research.md` - provider choice and decision gates
2. `memory-poc-everos.md` - evidence required before production integration
3. `memory-plugin-everos-phase1.md` - user-visible MVP contract
4. `memory-plugin-everos-phase1-tech.md` - implementation boundary and tests

## Version map

| Version | Location | Purpose |
|---|---|---|
| Original rev37 | `docs/plans/memory-*.md` | Full pre-convergence design baseline |
| Converged MVP | `docs/plans/memory-mvp/` | Review candidate with deferred capabilities removed |

The MVP POC is still marked `not run`. Review should distinguish agreement on
the product/technical boundary from evidence that EverOS passes the provider
gate.
