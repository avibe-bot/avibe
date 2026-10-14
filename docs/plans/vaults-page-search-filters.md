# Vaults list search and filters

## Change contract

The owner approved option A of the synthetic Vaults prototype on 2026-10-13.
The list remains client-side and value-free: every whitespace-separated query
term must match case-insensitively in explicitly selected metadata. Search,
intersection tag/skill selection, quick filters and sorting compose; the URL is
the view's source of truth and updates replace history without consuming browser
handoff parameters. The row retains tier/signing badges, caps inline tags at two
(skills first), exposes the rest to pointer and keyboard users, and keeps hosts
and tags visible at narrow widths. Metadata includes relative last use.

Boundaries: the Vaults hub, small list/search helpers, existing UI primitives,
and English/Chinese copy. No API, custody, approval or pending-request change.
Multi-field metadata is integrated after master `20664d45b`: named field labels,
  `NAME.field` references and the `NAME.<default>` alias are searchable, while
  envelopes/field values remain excluded. Field rows, corruption warnings,
  reveal selectors, stale-field handoffs, and refreshed field identity are kept
  alongside this lane's layout and URL filters.

## Reuse decisions

- Use Input, Popover, Checkbox, Select, SegmentedRadio and existing badge styles.
- Combobox/cmdk are single-selection pickers: use checkbox rows inside the
  existing Popover for persistent multi-selection, not a second combobox API.
- Use existing route-surface keyboard handling and relative-time formatting.
- Keep handoff parameter ownership in `vaultBrowserHandoff.ts`.

## Validation contracts

Each new test protects behavior absent from the existing audit/reveal coverage:

- Pure matcher table: AND/case/whitespace, skill names, hosts/auth, signing
  addresses and localized tier/kind labels; unexpected value/policy fields must
  not become searchable through accidental whole-object serialization.
- Page interaction: dropdown inventory counts (unique secrets), ordering and
  intersection; the former chip wall cannot masquerade as a dropdown.
- Row interaction: two inline tags, skill precedence, overflow revealed by hover,
  focus and click, and toggles reaching the same filter state.
- Quick filters/sort: protection/kind/null last-use and descending timestamps,
  with never-used last and names as deterministic tie-breakers.
- URL interaction: restored filters/query/order, replace navigation, and handoff
  step removal preserving view state (and view changes preserving the step).
- Search/empty state: literal highlights, keyboard shortcut, clear controls,
  inventory-empty copy versus a zero-match reset action.

Run focused Vitest through `~/.local/bin/hd1-test`, UI build and changed-file
lint; CI owns the broad suite. Never restart or probe the live local runtime.

## Validation evidence

- Focused Vitest: 113 passing cases across the matcher, Vaults page, browser
  handoff, and multi-field UI suites. Includes unchanged audit/reveal behavior,
  field-label/reference search, corrupt-field rendering, selected-field canonical
  reveal records and stale-field handoff checks.
- `npm run build`, `npm run typecheck:tests`, and baseline-aware `npm run lint`
  pass. No Python changes and no full local test-suite run.
- Hermetic Chromium sanity check: real page/components and CSS with 26 synthetic
  records and 55 distinct tags, all API responses intercepted, all non-fixture
  network requests blocked, backend fallback pointed to the closed discard port.
  Checked desktop at 1280px and mobile at 390px, no horizontal page overflow,
  visible mobile host/tag chips, tag-picker counts, and overflow keyboard entry /
  Escape focus return. Test-owned browser profile; no live service or real Vault.
- Screenshots (synthetic inventory, Chinese locale):
  [before](assets/vaults-search/before-desktop.png),
  [after](assets/vaults-search/after-desktop.png),
  [tag picker](assets/vaults-search/tag-picker.png),
  [mobile](assets/vaults-search/after-mobile.png).

## Known by design / residuals

- Backend find supplies the AND/case-insensitive semantics, not a wire protocol
  for browser search; localized labels extend the value-free UI haystack.
- Counts in the tag picker are whole-inventory counts, not cascading counts.
- Clear search/filters preserves the user's sort and unrelated URL parameters.
- An explicit reveal handoff clears search/tag/skill/quick filters only when
  they hide its target, preserves sort, then resumes the existing row menu.
  This exception was approved by the orchestrator; it adds no new UI.
- Public EN/ZH documentation is coordinated by the orchestrator in avibe-docs.
- No bulk actions or design.pen edits. Hub frame `y4rw5Q` needs Pencil follow-up.
- Mobile width was checked in hermetic Chromium; a real-device / local Incus
  manual acceptance check remains, including touch and the integrated shell.
