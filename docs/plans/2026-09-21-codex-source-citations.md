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
  Each absence carries the fingerprint that proved it and may suppress a re-read
  of that history only — a read directed at other refs walks the same file
  without learning anything about this one, so one shared "already read" mark
  would let it vouch for an absence it never tested.
- **One shared rewrite, two readings.** `core/citations.py` rewrites each
  marker into an ordinary Markdown link (`[domain](url)`) and returns a
  structured sidecar persisted at `message.content.citations`. Every IM
  platform delivers that Markdown through the renderer it already had — plain
  `domain (url)` where the platform has no hyperlinks, which is what WeChat's own
  formatter says a link looks like — and the Web transcript matches a link
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
- **Untrusted input.** Titles and URLs come from search results: a title has
  its controls and private-use characters stripped and is collapsed and bounded,
  a URL is cleaned the way a browser cleans one (see below), only http(s) with a
  host is accepted, and link labels are escaped. Whether a marker is
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
  lifted the URL out of HTML sends `&amp;`) using micromark's replacement table
  rather than HTML's — micromark is the parser behind this renderer, and it
  replaces the C1 range and the noncharacters with `U+FFFD` instead of applying
  the Windows-1252 mapping that would make `&#x80;` a `€` the rendered link
  never opens. The raw string is cleaned first, exactly the way WHATWG's URL
  parser cleans its own input, because that is what decides the page: an ASCII
  tab or newline is removed wherever it sits, leading and trailing C0 controls
  and spaces are trimmed, and everything else — a C1 control, a zero-width, a
  private-use code point — survives to be percent-encoded rather than deleted.
  Deleting it moved the destination silently
  (`https://example.com/p&#x80;q` became `https://example.com/pq`). That order —
  clean the literals, then resolve the references — is the only one that keeps
  both halves of the contract, because the two spellings are not the same
  character to a Markdown parser: a literal tab or newline *ends* a destination,
  so `[x](https://example.com/p<TAB>q)` renders as no link at all, while the
  same character written as `&Tab;`, `&#x9;` or `&#xA;` is part of the
  destination and comes out of the renderer percent-encoded as `%09` / `%0A`.
  Cleaning after resolution would have deleted it and pointed the persisted URL
  at `…/pq`, a page the badge never opens. Then
  percent-encoding that matches `normalizeUri` — existing `%XX` escapes
  preserved, lone surrogates repaired to `U+FFFD`, parentheses encoded so a
  Markdown destination cannot truncate, and the result a fixed point of itself —
  and the scheme lowercased, since a renderer that only knows the lowercase
  spelling sees no link at all (Telegram delivered
  `[example.com](HTTPS://Example.com/X)` as raw Markdown). Backslash escapes are deliberately *not*
  resolved: WHATWG reads `\` as a host separator where `urlsplit` reads
  userinfo, so it is preserved as `%5C`, and a canonical URL whose host the two
  parsers could still read differently is rejected outright rather than
  repaired. The label attributes the host a browser would actually reach:
  non-transitional UTS #46 per label (`例え.jp` → `xn--r8jz45g.jp`,
  `faß.de` → `xn--fa-hia.de`, where the standard library's IDNA 2003 codec would
  have said `fass.de` — a different domain than the link opens), with ASCII
  labels passed through as a browser passes them and a host that cannot be
  canonicalized rejected rather than guessed at. A host whose last label is a
  number goes through WHATWG's IPv4 parser for the same reason, so
  `2130706433`, `0x7f.1`, `127.1` and `017700000001` are all attributed to
  `127.0.0.1` instead of hiding a loopback destination behind the digits that
  spell it, and a form the parser refuses (`256.1.1.1`, `1.2.3.4.5`,
  `example.com.0x1`) names no host and is rejected. A number written wider than
  32 bits is refused as a host rather than converted, which also keeps CPython's
  4300-digit bound on decimal `int()` off an untrusted host: `https://9…9/x`
  with five thousand digits names no host instead of raising. A host too long for
  a label is elided from the **left**, keeping the host's tail verbatim — the
  label is always the last 63 characters behind an ellipsis, so it is either the
  host or a suffix of it. `developers.openai.com.<padding>.attacker.example` is
  shown as a tail that ends in `.attacker.example`, never as
  `developers.openai.com…`, which would name a site the link never opens; and
  never as `…co.uk` either, which keeping whole labels only would have produced
  for a 62-character label under `co.uk` — a public suffix every site beneath it
  shares, and the perfect place to hide a long attacker label. Only the leftmost
  piece may be partial, and a partial piece cannot read as a different domain:
  it is short only when whole labels already fill the budget, and those are the
  rightmost ones, so the registrable domain is shown whole. So neither an unsafe
  URL nor an ordinary unrelated link can become a fabricated citation. One fixture,
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
  absent or unreadable history that later becomes readable, a ref proven
  absent not being rescanned, and an absence not outliving the history that
  proved it), the URL identity table including the IDN mapping cases, the real
  `WeChatBot.format_markdown` boundary rather than its formatter alone, message
  readiness at
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
