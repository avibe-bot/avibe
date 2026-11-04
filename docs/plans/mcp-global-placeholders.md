# Avibe-owned global MCP placeholders

## Change contract

Global Avibe-owned servers reuse the project validator, resolver, template/private
map split, and Configure flow. `${VAR}` is accepted in URLs, env values and header
values, never command or args. Native imports are unchanged.

Templates remain in the existing global `definition.json`; concrete supplied
values live in a private `secrets.json` sidecar in the same directory. New adds
split every map per key, including concrete-only maps. Replacements preserve
unsupplied keys; supplied keys move to their new destination and leave no copy in
the other. Complete prospective state is validated before writing. Configure
retains the existing whole-map replacement semantics.

Unresolved additions persist without connecting. They are visible as
`needs_config` but excluded from the prompt index and blocked from calls.
Configuration resolves privately in memory and refreshes the server; published
artifacts and public summaries retain templates/redaction, never private values.
Released definitions without placeholders retain existing read behavior; no
schema migration, new directory layout or native placeholder behavior is added.

## Boundaries and evidence

- Shared backend: add parsing, definition/cache persistence, resolution,
  configuration, discovery, publishing and execution.
- Web: existing Configure dialog and API method reused for global Avibe rows;
  inherited global rows in a project remain read-only, native rows unchanged.
- Owner-boundary tests: lifecycle, mixed/concrete maps, replacement destination
  moves, invalid inputs without writes, legacy fixture, private output/artifacts.
- UI test: global needs-config row opens Configure and saves without project ID.
- Chrome fixtures: 1192px and 390px content widths, needs-config row and open
  Configure dialog, no viewport-wide horizontal overflow.
- Delivery requires exact-head Codex clean, zero unresolved review threads and
  green CI. The owner retains merge responsibility.

## Intentional non-changes

Literal secrets embedded directly in URL strings are not masked by this change.
Native definitions and existing legacy literal-secret behavior remain compatible.
Project publication behavior is not redesigned here; the global implementation
must reuse shared mechanisms without claiming to remediate all prior project
artifact handling.

## Publication lifecycle and accepted concurrency window

Persisted Avibe definitions own management visibility, independently of a
published catalog. A configured server whose discovery fails remains visible as
`not_generated`, with Configure, Refresh and Remove available, but no callable
tools. Missing variables instead produce `needs_config`.

All secret-changing owner mutations invalidate/retire the current publication
through one shared path before activating new sidecar values. Until refresh
publishes again, new admissions cannot use the previous tools.

Known by design: an agent call that already read the old publication when the
owner reconfigures the same server may continue with whatever secret it already
resolved. This narrow concurrent-start window is explicitly accepted by the PM;
there is no revision-secret snapshot binding or admission-counter mechanism.
