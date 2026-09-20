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
  bounded memo; the record is the rollout history Codex itself writes. A
  completed search is recorded under two names, so the reader accepts both: an
  older rollout names the item `WebSearch`, while the shipped codex-cli 0.154.0
  records it as `Extension` carrying `kind: "web.search"` (the live notification
  normalizes both to `webSearch`). Both are in real history — measured locally,
  1023 old-form rows carrying results against 267 new-form ones — and every row
  the current binary writes is the new one. A thread that reaches this process
  with no live searches — after a restart, a resume (`excludeTurns`), a re-read
  (`includeTurns: False`), or a `thread/fork` that returns only an id — is read
  back from that history, located through Codex's own `threads.rollout_path`
  index rather than a guessed filename. Scope is the rollout *file*: a fork's
  file opens with the parent's rows still carrying the parent's `thread_id`, so
  the fork inherits exactly the history it carries.
  What decides whether to read is the refs the message in hand actually cites,
  never whether the thread has an entry — a partially populated thread is still
  missing exactly the refs a resume or a restart lost. That also bounds the read
  by the message instead of by a cap, so no limit can discard the one old ref
  being resolved, and an eviction costs a re-read rather than the attribution.
  A read only licenses "this ref is absent" when it reached the end of a file
  that did not change underneath it: an unreadable, unindexed, or still-growing
  history is re-read later rather than settling into permanent negative truth.
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
- **An intermediate message waits for its own sources, and only for them.** A
  `webSearch` item and the `agentMessage` that cites it are separate
  notifications, so a narration message whose ref has no source recorded yet is
  queued rather than delivered with a label it would never lose. The wait is
  bounded by the turn: the queue drains as soon as the defining search arrives,
  and is force-flushed at the turn's terminal boundary — completion, failure,
  interruption, or a non-retrying `error` — so a ref that never arrives still
  ships with its visible label. Every intermediate emit passes through the same
  queue, so holding one message cannot let the messages behind it change places,
  and a message that cites nothing is never delayed by anything except messages
  already ahead of it. A delivered message is never rewritten afterwards, and a
  superseded turn discards its queue exactly as it discards its result
  candidate.
- **Untrusted input.** Titles and URLs come from search results: controls and
  private-use characters are stripped, titles collapsed and bounded, only
  http(s) with a host accepted, and link labels escaped. Whether a marker is
  code is a CommonMark question — fence lengths nest, an unclosed fence runs to
  the end, a code span may cross lines, container indentation shifts all of it —
  so the rewrite reuses the offset-preserving mask already shipped for `$<NAME>`
  secret requests instead of lexing code a second time
  (`core/reply_enhancer.mask_hidden_and_code`, which extends it with `<silent>`
  blocks: those are removed before delivery, so a marker inside one must not
  spend a citation index or lift a URL out of the block that hid it). An answer
  that explains this grammar keeps every byte of its examples.
- **One URL on both sides of the boundary.** The Web transcript matches a
  rendered `href` against the sidecar, so the persisted URL has to be the exact
  string the Markdown renderer produces. The backend therefore writes the
  canonical form up front: character references resolved once (a provider that
  lifted the URL out of HTML sends `&amp;`), then percent-encoding that matches
  `normalizeUri` — existing `%XX` escapes preserved, lone surrogates repaired to
  `U+FFFD`, parentheses encoded so a Markdown destination cannot truncate, and
  the result a fixed point of itself. Backslash escapes are deliberately *not*
  resolved: WHATWG reads `\` as a host separator where `urlsplit` reads
  userinfo, so it is preserved as `%5C`, and a canonical URL whose host the two
  parsers could still read differently is rejected outright rather than
  repaired. The label attributes the host a browser would actually reach,
  IDNA-encoded (`例え.jp` → `xn--r8jz45g.jp`), so neither an unsafe URL nor an
  ordinary unrelated link can become a fabricated citation. One fixture,
  `tests/fixtures/citation_url_identity.json`, is asserted by both the Python
  canonicalizer and the real renderer.
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
  ref isolation, both recorded item shapes in one thread, a partially populated
  thread, a bound that must not discard the ref being resolved, an empty or
  absent or unreadable history that later becomes readable, and a ref proven
  absent not being rescanned), message readiness at
  every terminal boundary, hidden blocks, event ordering and repeated
  completions, IM delivery, persistence and reload.
- `tests/e2e/test_codex_citation_contract.py` — the installed `codex` binary
  relays markers byte-for-byte, and its real notifications drive the product
  chain (event handler → `emit_result_message` → dispatcher → SQLite) to a
  delivered message and a persisted sidecar. Both halves are real bytes,
  including `results[]`: the binary's standalone search tool posts to
  `<provider base_url>/alpha/search`, so the loopback provider answers the
  search itself once `model_providers.<p>.supports_standalone_web_search` and
  the under-development `features.standalone_web_search` are set, and the
  `webSearch` item the test replays is the captured native notification.
- `ui/src/components/ui/markdown.test.tsx` — renderer matching, degradation to a
  plain link, hover/focus/touch behavior, and accessible naming.
- `ui/e2e/citations/` — real-browser layout and pointer routing on desktop,
  mobile Chromium, and mobile WebKit, in English and Chinese, including the
  falsifiable form of the no-fetch rule: zero requests to the cited site after
  render and preview, exactly one after an explicit open.

## Known-by-design

- **A ref whose search never arrives still degrades to a label.** Holding an
  intermediate message is bounded by its turn, not by a promise about ordering:
  when the turn reaches a terminal boundary with the ref still undefined, the
  message ships with the visible unresolved label. Nothing is re-resolved or
  backfilled after delivery, because rewriting a message after the fact would
  let an old answer change under the reader.
  The corpus measurement that once justified doing nothing here is retained as
  frequency evidence, not as a protocol invariant: across 344 rollout files,
  8,893 cited refs had a `WebSearch` row in the same file and **0** of them
  appeared after the citing row. Nothing in the transport enforces that, so the
  queue above covers the inversion rather than assuming it away; the history
  read still covers the commoner case where the row exists but this process
  never saw the live event.
- **A final result message is not queued.** `turn/completed` is already the
  boundary the queue would wait for, so the turn's result is resolved and
  delivered there with whatever attribution exists at that moment.
