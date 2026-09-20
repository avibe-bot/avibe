# Memory profile: progressive disclosure for explicit-info / implicit-trait rows

## Background

Owner session `sesehz3dgw5ua` approved a displayed interaction mockup on
2026-09-09 ("按这个样式实现"). This document is the copied product contract for
the resulting frontend-only PR. Delivered by a delegated implementation lane;
the dispatching PM session (`sesehz3dgw5ua`) owns integration, merge, and
deploy decisions.

`StructuredMemoryProfile` (in
`ui/src/components/settings/memory/MemoryProfilePanel.tsx`) renders a memory
profile's `summary`, `explicit_info` (category/description/evidence) and
`implicit_traits` (trait/description/basis/evidence). Every entry currently
renders fully expanded; this task adds independent progressive disclosure per
entry, entirely on the frontend.

## Goal

Each explicit-info and implicit-trait entry becomes independently collapsible,
initially closed, without any backend/API/EverOS change, new dependency, or
global/persisted state.

## Approved UI contract

1. Each explicit-info and implicit-trait entry is independently collapsible,
   initially closed. Closed row shows ONLY the existing category/trait label
   styled consistently with the existing `Badge`, plus an expansion chevron.
   The full row is the clickable/keyboard target, with a visible focus ring
   and correct disclosure semantics (`aria-expanded` on the trigger). Long
   Unicode titles remain usable on mobile with no horizontal overflow.
2. Expanding a row reveals the unchanged description and existing basis, then
   an independently initially-collapsed evidence disclosure labeled with the
   existing localized evidence key (`memory.profile.evidence`, i.e. 相关证据 in
   zh). Opening a row must NOT auto-expand its evidence. Evidence
   absent/empty ⇒ no evidence toggle at all. No nested
   button-inside-button/invalid `<summary>` controls.
3. Multiple entries may be open simultaneously; toggling one entry or its
   evidence must not affect siblings. Implemented with local component state
   (`useState` per row) — the smallest existing/native disclosure primitive,
   matching the button+chevron+`aria-expanded` idiom already used in
   `ui/src/components/workbench/WorkbenchSidebar.tsx`. No new dependency, no
   global state, no persistence. State lives only for the mounted entry;
   remounting (e.g. after a profile reload) initializes closed again.
4. Missing or whitespace-only `category`/`trait` gets a localized numbered
   fallback (`memory.profile.entryFallback`, "Profile entry {{number}}" /
   "画像条目 {{number}}"), numbered deterministically 1..N within its own
   section (explicit-info numbering is independent from implicit-traits
   numbering) so every entry stays discoverable. Provider data is never
   mutated — the fallback is display-only.
5. Overall profile `summary` stays expanded and unchanged. Card
   metadata/date/origin and section headers are unchanged. Legacy
   unstructured `item.text` rendering is unchanged (no parsing of arbitrary
   text into sections). Provider strings remain inert text (never
   markdown/HTML). No truncation of descriptions/evidence, no ID/date
   rewriting.

## Non-goals / explicit exclusions

- No backend, API, or EverOS changes.
- No new UI dependency, no accordion library.
- No persisted/global expand state (no localStorage, no context).
- No redesign — reuse existing Badge, existing button/chevron disclosure
  idiom, and existing Tailwind tokens already validated against
  `avibe-docs/design.pen`.

## Scenario catalog note

`tests/scenarios/memory_search/catalog.yaml` covers backend admission/store/
session-lifecycle behavior (`tests/test_memory_admission.py`,
`test_memory_store.py`, etc.). It has no scenario IDs for frontend rendering
and is not the applicable catalog for this change. No scenario IDs are added
there; coverage for this task lives in focused component tests colocated with
the component, per existing precedent in this directory
(`MemoryProfilePanel.test.tsx`, `MemoryProcessingRecordPanel.test.tsx`).

## Implementation notes

- New collapsible row component(s) local to `MemoryProfilePanel.tsx`, built
  from a plain `<button type="button" aria-expanded=... onClick=...>` +
  `ChevronDown`/rotate idiom (same as `WorkbenchSidebar.tsx`), not a new
  primitive under `ui/src/components/ui/`.
- Evidence sub-disclosure reuses the same idiom, independently keyed per row.
- Fallback label copy added to `ui/src/i18n/en.json` / `zh.json` under
  `memory.profile.entryFallback`.

## Test plan

- Initial collapsed state (label only, no description/basis/evidence
  visible).
- Title-trigger open/close (click + keyboard).
- Evidence independently closed/open after row expands; opening a row does
  not auto-open evidence.
- Siblings independence (two entries toggled independently).
- Category and trait sections both covered.
- Fallback numbering, including whitespace-only category/trait and long
  Unicode titles.
- Empty/absent evidence ⇒ no evidence toggle rendered.
- Basis rendering preserved (implicit traits).
- Summary and legacy `item.text` rendering preserved.
- Hostile/text-inertness preserved (existing XSS-inert assertion kept).

## Residuals

- Lightweight browser verification (synthetic long Chinese content, desktop +
  mobile widths) reported separately in the PR/final report using sanctioned
  hermetic tooling only — no host service install/restart, no production
  state.
