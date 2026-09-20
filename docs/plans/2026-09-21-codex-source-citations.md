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
  Ref tokens genuinely repeat across threads (1286 of 2466 distinct tokens in
  the local corpus appear in more than one thread), so a thread reads only its
  own sources.
- **The live stream is a fast path, not the record.** The in-process map is a
  bounded memo; the record is the rollout history Codex itself writes. A thread
  that reaches this process with no live searches — after a restart, a resume
  (`excludeTurns`), a re-read (`includeTurns: False`), or a `thread/fork` that
  returns only an id — is read back from that history, located through Codex's
  own `threads.rollout_path` index rather than a guessed filename. Scope is the
  rollout *file*: a fork's file opens with the parent's rows still carrying the
  parent's `thread_id`, so the fork inherits exactly the history it carries. An
  eviction costs a re-read, never the attribution.
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
  nothing to fall back on keeps its raw text. A history read that fails logs the
  path it failed on and degrades the same way. Old messages stored before this
  change are not backfilled.
- **Untrusted input.** Titles and URLs come from search results: controls and
  private-use characters are stripped, titles collapsed and bounded, only
  http(s) with a host accepted, Markdown link punctuation percent-encoded, and
  link labels escaped. Whether a marker is code is a CommonMark question — fence
  lengths nest, an unclosed fence runs to the end, a code span may cross lines,
  container indentation shifts all of it — so the rewrite reuses
  `core/reply_enhancer.mask_markdown_code`, the offset-preserving mask already
  shipped for `$<NAME>` secret requests, instead of lexing code a second time.
  An answer that explains this grammar keeps every byte of its examples.
- **Surface rules.** The badge is the link itself — hover or focus reveals the
  preview, the first touch reveals instead of navigating, `Enter` opens the
  page, and external links keep `noopener noreferrer nofollow`. Only an
  agent-authored reply may draw a badge; a user bubble or a non-interactive
  preview renders the plain domain link.

## Verification

- `tests/test_citations.py`, `tests/test_codex_citations.py`,
  `tests/test_im_citation_delivery.py`, `tests/test_message_mirror.py` — grammar
  and escaping boundaries (including non-ASCII titles, nested containers, and
  legal fence lengths), thread scoping and eviction, history recovery on the
  real consumption path (fresh handler, cross-turn, fork inheritance, same-name
  ref isolation, unreadable and partially written files), event ordering and
  repeated completions, IM delivery, persistence and reload.
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

## Known-by-design

- **A search whose result arrives after the answer that cites it degrades.**
  `item/completed` for a `WebSearch` and for the `AgentMessage` that cites it are
  separate notifications, so nothing in the protocol forbids the citing message
  from being delivered first. When that happens the marker resolves to the
  visible unresolved label, not to a guess — the same degradation an unknown ref
  already takes. It is not deferred, re-resolved later, or backfilled: holding a
  delivered answer to wait for metadata that may never come would trade a
  visible gap for a stalled reply, and rewriting a message after the fact would
  let an old answer change under the reader.
  Measured on the developer's own rollout corpus before accepting this: across
  344 files, 8,893 cited refs had a `WebSearch` row in the same file and **0**
  of them appeared after the citing row — the searches Codex cites are recorded
  before the message that cites them. The history read covers the case that does
  occur in practice (the row exists but this process never saw the live event);
  ordering inversion is left to degrade visibly rather than guessed at.
