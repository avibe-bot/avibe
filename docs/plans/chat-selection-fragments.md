# Range-bounded Markdown copy

## Decision

The selection toolbar must not silently widen Copy to whole paragraphs, lists,
code blocks, tables, or bubbles. Selection extent takes priority over preserving
formatting. This replaces the whole-block partial-copy policy from PR #2371.

The user explicitly accepts dropping formatting cut by either selection edge;
we must not complete wrappers or import surrounding text to make the fragment
self-contained. Drop incomplete **formatting**, never selected content.

For `Before **bold words** after`:

| Selected visible content | Clipboard |
| --- | --- |
| `bold words` (entire emphasis content) | `**bold words**` |
| `old wor` (inside emphasis) | `old wor` |
| `words after` (starts inside emphasis) | `words after` |
| `Before bold words` | `Before **bold words**` |

## Invariants

- No unselected visible text is copied. Touching every block is not equivalent
  to selecting the whole bubble.
- Preserve original Markdown for formatting constructs entirely covered by the
  selection. If a construct is clipped, omit its own syntax and process only
  its selected contents; a complete nested construct may retain its formatting.
- Do not synthesize missing open/close delimiters, enclosing containers, or
  unselected list items/table cells/code lines. Plain selected text is the safe
  fallback for a clipped/unsupported construct.
- Preserve selected whitespace, non-ASCII, emoji, and line breaks appropriately;
  do not use rendered-text substring searches to guess source offsets.
- Link labels partially selected remain just the selected label text; a complete
  inline link may retain its Markdown destination. Do not append unselected
  footnote bodies or other visible definition text to a partial selection.
- A genuine full-bubble selection (including Select all) retains byte-exact full
  Markdown source. Copy across bubbles takes only each selected fragment.
- Images/rules completely selected retain their Markdown. Selection endpoints at
  the next block/bubble's start must not pull that block/bubble into the copy.
- Quote/Ask semantics and toolbar gesture/placement behavior remain unchanged.

## Architecture direction

Reuse the renderer/parser's existing source spans. Prefer a conservative tree
walk: retain source for fully selected supported subtrees, recurse into clipped
ones, and use only the selected text for clipped leaves. Do not reintroduce the
old heuristic character alignment, indentation reconstruction, or arbitrary
Markdown grammar repair that made PR #2371 repeatedly fail review. If the current
marks cannot establish a safe mapping, improve the owning renderer annotation
or fall back locally to plain selected text, not whole-block expansion.

## Verification and scope

- Rework the existing markdownSource table-driven tests around the new policy;
  demonstrate the partial-selection regression fails on the old implementation.
- Include formatting edges, nested inline formatting, links, CJK/emoji, escapes,
  multiline selection, list/table/code slices, definitions, mention chips,
  textless selections, multiple bubbles, and genuine whole-bubble byte identity.
- Toolbar tests must consume the fragment through the real clipboard boundary.
- Use hermetic browser validation for browser Range behavior that jsdom misses;
  no production service restart or real user state/profile writes.
- Ship independently from PR #2426 (notice layout and touchend handling), based
  on default-branch interfaces. No stacking or cherry-picking that PR.

## Implemented boundary

`ui/src/lib/markdownSource.ts` walks the rendered DOM of each bubble the
selection reaches, reading the `data-md-start` / `data-md-end` spans the
renderer already writes. It adds no source-offset alignment and no Markdown
repair:

- An element whose whole visible content (non-blank characters, images,
  rules) lies inside the selection copies its source span, mapped back through
  the mention/secret rewrite passes. The bubble root is the first such element,
  which is what makes a whole-bubble selection byte-exact.
- Any other element is walked into. Its text copies as the selected DOM
  characters, so an escape or entity in a cut run copies as the character it
  shows.
- Table rows and cells never copy their own span (a cell's span starts at its
  pipe); cut cells are separated by a tab and rows by a line break. A whole
  table copies its source.
- Inside a cut quote or list item, a covered element whose source spans
  several lines is walked into instead, because those lines carry the
  container's `>` or indentation. Single-line sources still copy as written.
- Parts from different blocks are joined by one line break or a blank line,
  following the newline count written between the two blocks.
- Renderer-only text outside every source construct (the footnote section's
  label) and the blank layout text between blocks are never copied.
- A covered reference link appends its `[id]: url` definition, which renders
  nowhere. Footnote bodies are never appended.

Browser Range behavior (double-click, triple-click, hit-tested drags, Select
all) is covered by the hermetic `npm run test:chat-selection` harness.
