# Codex Source Citations

Codex answers a web-searched question with opaque inline markers instead of
links: `U+E200 cite U+E202 <ref_id> [U+E202 <ref_id> …] U+E201`, where each
`ref_id` names one result of a `webSearch` item the same native thread already
reported. Delivered verbatim, a reader sees an unreadable control-character
blob with no way to reach the page the answer is based on.

## Contract

- **Resolution is local.** The sources are the `results[]` Codex already sent.
  Citation handling performs no search, no page retrieval, no favicon or
  metadata fetch, and no additional model call.
- **Scope is the native thread.** A `ref_id` is unique only inside its own
  Codex thread, so harvested results are keyed by thread id, never globally and
  never per Avibe turn — a later turn may cite a search run in an earlier one.
  The cache is bounded and is not durable truth.
- **One shared rewrite, two readings.** `core/citations.py` rewrites each
  marker into an ordinary Markdown link (`[domain](url)`) and returns a
  structured sidecar persisted at `message.content.citations`. Every IM
  platform delivers the Markdown unchanged; the Web transcript matches a link
  against the sidecar and upgrades it to a compact numbered badge with a
  title/domain preview. Neither surface re-parses markers, and the sidecar adds
  no attribution the text does not already carry.
- **Never invent, never silently drop.** A URL is never derived from a `ref_id`
  or from search order. An unknown, malformed, or non-http(s) source degrades to
  a visible localized label (`message.citationUnresolved`); a marker with
  nothing to fall back on keeps its raw text. Old messages stored before this
  change are not backfilled.
- **Untrusted input.** Titles and URLs come from search results: controls and
  private-use characters are stripped, titles collapsed and bounded, only
  http(s) with a host accepted, Markdown link punctuation percent-encoded, and
  link labels escaped. Markers inside inline, fenced, or indented code stay
  literal, so an answer that explains this grammar still renders it.
- **Surface rules.** The badge is the link itself — hover or focus reveals the
  preview, the first touch reveals instead of navigating, `Enter` opens the
  page, and external links keep `noopener noreferrer nofollow`. Only an
  agent-authored reply may draw a badge; a user bubble or a non-interactive
  preview renders the plain domain link.

## Verification

- `tests/test_citations.py`, `tests/test_codex_citations.py`,
  `tests/test_im_citation_delivery.py`, `tests/test_message_mirror.py` — grammar
  and escaping boundaries (including non-ASCII titles), thread scoping and
  eviction, event ordering and repeated completions, IM delivery, persistence
  and reload.
- `tests/e2e/test_codex_citation_contract.py` — the installed `codex` binary
  relays markers byte-for-byte, and its real notifications drive the product
  chain (event handler → `emit_result_message` → dispatcher → SQLite) to a
  delivered message and a persisted sidecar. Marker relay is proven on real
  bytes; `results[]` is schema ground truth, because the hosted search service
  behind it cannot be produced by a loopback provider (see
  `_tmp/codex-citation-verification-20260919.md`).
- `ui/src/components/ui/markdown.test.tsx` — renderer matching, degradation to a
  plain link, hover/focus/touch behavior, and accessible naming.
- `ui/e2e/citations/` — real-browser layout and pointer routing on desktop,
  mobile Chromium, and mobile WebKit, in English and Chinese, including the
  falsifiable form of the no-fetch rule: zero requests to the cited site after
  render and preview, exactly one after an explicit open.
