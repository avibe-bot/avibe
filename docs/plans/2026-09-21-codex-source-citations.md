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
  formatter says a link looks like — and the Web transcript recognizes the links
  the rewrite wrote and upgrades those to a compact numbered badge with a
  title/domain preview. Neither surface re-parses markers, and the sidecar adds
  no attribution the text does not already carry.
- **A badge belongs to a link the backend wrote.** A badge is an attribution
  claim, so recognizing one is a provenance question rather than a text
  question: an answer may word its own sentence around the same page, and that
  link is the one nobody vouched for. Matching a rendered label back to a
  sidecar row cannot tell the two apart — and neither can the marker grammar,
  once delivery has run. Every stage between the model's text and the reader's
  rewrites that text, and a rewrite can splice two ordinary halves into
  something that reads as a marker. Removing a `<silent>` block turns
  `\ue200ci<silent>x</silent>te\ue202turn0view0\ue201` into a complete marker;
  flattening a `file://` attachment to its label does it too, and two such
  deletions in one reply form a marker in the IM body that the workbench body,
  which keeps the attachment, never forms. A stage that asked the delivered
  text which markers to attribute would answer with both, and would hand the
  one the model never wrote the other one's link.
- **So identity is registered once, where the grammar still means what it
  says.** `register_citations` runs at the native input boundary, over the text
  exactly as the backend produced it. It uses the same offset-preserving
  mask the rewrite always used, widened to cover the Markdown slots a citation
  cannot be shown in (`core/reply_enhancer.mask_citation_slots`), resolves every
  complete marker it may attribute — including the ones that end in the
  unresolved label — and
  replaces each with an opaque per-occurrence token: a 128-bit nonce, never
  derived from the ref or from its order, and checked against the arriving text
  so a token can only ever be one this call minted. The bundle it returns
  carries those registrations and a snapshot of the sources they consulted.
  Delivery then transforms text that contains tokens, and every text-only exit
  **materializes** them — the IM copy (before length limits, truncation and
  splitting), the Turn snapshot, the agent-run record a Harness caller reads
  back, the tool trace, the notify/intermediate/suppressed paths, the SSE
  chunk, and the quick-reply and file labels extracted out of the body, which
  are not Markdown anywhere and get the bare domain instead. A token that never
  reaches an exit is simply not a citation; a token copied within the text still
  stands for the citation it named; text spliced together out of the leftovers
  cannot become one. A token that has landed inside code or a hidden block by
  the time delivery settles gets its original marker text back, rather than a
  link or an exposed internal token. Markers that were already in code or
  hidden blocks are never registered, and a native code literal, a truncated
  marker, and a visible unresolved fallback all keep the behaviour they had.
- **A citation is only written where a link can be shown.** CommonMark has no
  link inside a link, so a source spliced into a label takes the enclosing unit
  down with it: `[Docs MARK](https://openai.com/docs)` rendered as a link
  nested in its own label, and the reader was shown brackets instead of the
  page. A marker written into a destination, a title, a reference identifier or
  an angle autolink's address is worse than that — those characters are what
  makes the unit reach somewhere, and splicing a link through them sends it to
  an address nobody wrote. So the parser decides, on the offset-preserving
  CommonMark units `markdown_link_units` already reports: a marker standing
  where a reader is shown something (a visible inline-link label, an explicit
  full-reference label, an image's alt text) is eligible, and its attribution
  is written immediately **after the outermost enclosing unit** — an image
  inside a link is two units, and only the link has an end a sibling can
  follow — in occurrence order, with a separating space unless one is already
  there. Only the registered token is removed from the label; the destination
  and the label's other text are untouched. A marker in a data or syntax slot
  is left exactly as the model typed it: no token is minted, no source is
  fetched, no sidecar row is emitted, and — because `citation_ref_ids` asks the
  same mask — delivery does not wait for a source it could never show. A
  shortcut or collapsed reference is the dual-use case, both the words a reader
  sees and the identifier that finds the address, so it and its definition are
  preserved whole — including when the marker is what keeps the run from
  resolving. CommonMark cannot match `[docs <marker>]` against `[docs]`, reads
  the brackets as prose, and would hand the run over as ordinary label text;
  the marker is not part of that identifier though, because citation delivery
  replaces it before a reader sees any of this. So the same parser is asked the
  same question a second time with the caller's own text — its markers and the
  tokens standing for them — blanked to spaces, which preserves every offset
  and normalises out of an identifier exactly as the marker would have been
  absent. Runs the text already parses inside a unit are left to the first
  answer: CommonMark has no link inside a link, so a bracket run in a label is
  shown text and nothing more. The decision is re-asked of the final text for
  this bundle's
  own tokens, so a token a transform moved into a label follows the rule above
  and one moved into code, a hidden block or a data slot gets its original
  marker text back. A structured field is not Markdown on any surface: a file
  label or a quick-reply button keeps its plain-domain attribution in place,
  with no link syntax and no token, because there is no unit to stand after.
  The sidecar counts only the links actually written as standalone citation
  links, and binds the body after that relocation.
- **A span is an identity only together with the body it was measured in.** The
  persisted body is finalized the same way, and each sidecar row carries
  `spans` — the exact ranges its links occupy — and `body_sha256`, one digest
  of that exact body, the same value on every row of the sidecar. Ranges alone
  can be impersonated: with `L = [example.com](https://example.com/x)`,
  `L + "\n\n" + L` measures `[0,36]` and `[38,74]`, and deleting the first
  paragraph leaves an ordinary link occupying `[0,36]` exactly. So the consumer
  verifies the digest against the full stored body first and reads the ranges
  only after it matches. Coordinates are UTF-16 code units, half-open. The
  digest is SHA-256 over the body's UTF-16LE code units, hex lowercase —
  `hashlib` on the backend, `@noble/hashes` in the browser (promoted to a direct
  dependency at the version the lockfile already pinned) — and the two agree on
  CJK, non-BMP emoji, CRLF and an unpaired surrogate. It says which version of
  a body a measurement belongs to; it is not a signature and claims nothing
  about where the body came from.
- **The reader verifies the stored body, then maps through the edits it made.**
  The Web transcript is not handed the row: `resultFooterParts` splits off a
  folded or structured result footer, and the Markdown renderer rewrites
  `@<…>` / `#<…>` mentions and `$<NAME>` secret requests into links before
  parsing. Each of those is a replacement with a measured length change, not an
  insertion, so each reports what it changed as a `TextEdit` in its own input
  coordinates and the binding is carried through in the order the passes ran —
  no pass is inferred from the text looking like a prefix of another. An edit
  that reaches into a citation's own link invalidates that citation rather than
  guessing where it moved. A badge is then drawn only where a span equals a
  parsed link node exactly. A missing, malformed or mismatched provenance
  field, an out-of-range or overlapping span, or a span that is not one whole
  link drops the entire new-contract set to plain links; it never falls back to
  matching text. Only a row carrying no provenance fields at all — persisted
  before this existed, and a shipped surface — is still recognized by its
  `(destination, label)` pair.
- **A link is held whole, and the platform spells it again.** A destination is
  data, not prose, and holding only the destination was not enough: the
  brackets left standing in the stream are still something a line scanner pairs
  up, and Slack's converter read `[^f]: [p](https://example.com/x)` as one link
  labelled `^f]: [p`. So the formatter layer holds each whole link and asks the
  platform to re-spell it from `render(label, destination)` — for Slack,
  `<url|label>`. That also repairs `[docs](url "T")`, whose title used to be
  concatenated into the destination and produced a link nobody could open. What
  a consumer scans is what the scan has to cover, so `inline_links` also reads
  the lines CommonMark hands to no inline parser — a link reference or footnote
  definition, an HTML block — one line at a time, which is the resolution those
  consumers read them at; lines a code block or another block already claimed
  stay out, so a code span written across two lines is still one code span and
  not a link found inside it. The spans come back sorted and non-overlapping,
  so a caller splices each exactly once. Code, images, and ordinary
  non-citation formatting keep the behaviour they had.
- **A link stays whole after the hold, too.** Holding the unit is where a
  platform reads it; two stages downstream still rewrite it. A result longer
  than one message is cut at whitespace on Discord and WeChat, and a cut taken
  through `[label](url)` delivers neither half — both chunks send successfully,
  so nothing falls back, and the reader is shown raw Markdown with no way to
  reach the source. The split now asks the same `inline_links` enumeration
  where a link is and chooses a boundary no link straddles; non-link text keeps
  the boundaries it had. Whether a link can be kept is decided against what one
  message actually holds, never against the preferred whitespace: a boundary is
  drawn before the link so it rides the next message whole, or — when the link
  begins the chunk — after it, because a chunk that is nothing but a link that
  fits is still a legal message, and the search then resumes past it so the
  next link is seen too. Only a link longer than a whole message has no
  boundary left. A link scan that fails answers "unknown", not "no links", so a
  plan that could not see a link never certifies one.
  When no boundary can save a link the split is abandoned **before the first
  chunk is sent** and the complete result goes out as the `result.md`
  attachment the fallback already had. WeChat has no `upload_markdown` — it
  inherits the base class's `NotImplementedError` — so that fallback also takes
  the ordinary `upload_file_from_path` route it does implement, staging the
  text in a temporary file that is removed once the upload returns; an adapter
  that reports failure with an empty id has delivered nothing, and the turn
  says so rather than claiming an attachment. The same answer is honoured by
  the plan's other consumer: an intermediate message on a platform that cannot
  edit one (WeChat) takes the unconsolidated log path, and it now takes that
  same whole-document route before sending any fragment, while staying an
  intermediate message — no terminal settlement, no result lifecycle signal, no
  transcript row of its own, and no notice copy.
  And at Slack's own serialization boundary, `&`, `<` and `>` in a label are
  encoded as Slack requires: a bare `>` closes `<url|label>` at that character,
  so the rest of the label and the whole address arrive as plain text. The
  label is finished before the wrapper closes over it, in the order that keeps
  Markdown's two spellings of a literal character apart: character references
  are resolved once after the platform converter ran (so `&copy;` is the `©` a
  reader sees, and `&amp;copy;` is the text `&copy;`), then the escape
  placeholders are restored — what a backslash protected is literal text that
  merely looks like a reference — and only then is every remaining `&`, `<` and
  `>` encoded, unconditionally. So `[a&amp;b]` and `[a\&amp;b]` reach Slack as
  two different strings and each reader sees what CommonMark shows; a reference
  inside a code literal stays written out. A citation of
  `https://a%26amp%3B.example/x` is exactly this case: the host contains
  `&amp;` literally, and the reader must not be told the page is on
  `a&.example`. Which spellings are references, and what each one stands for,
  are the parser's own questions — two thousand names, seven decimal digits or
  six hexadecimal ones, and U+FFFD for a code point it recognizes but cannot
  encode — so the parser's own inline rule is what answers them, run at each
  candidate offset outside code. A pattern that merely resembles the grammar,
  paired with a same-package decoding utility that takes eight digits and keeps
  an invalid spelling, is a second grammar that disagrees with every other
  Markdown surface. And because a reference spells a line break too (`&#10;`,
  `&#xA;`, `&NewLine;`, `&#13;`), the label's existing one-line rule — a
  newline inside `<url|label>` is not a link — is applied once more after
  interpretation, where the label's characters are finally known; an escaped
  `\&NewLine;` is literal text with no break to fold.
  The destination is finished in the same place and for the same reason. It is
  the other half of a wrapper built out of `<`, `|` and `>`, and an address is
  allowed to contain all three: one of them arriving raw ends the unit where
  the author did not, and the reader is shown a truncated link with the rest of
  the address beside it as text. What it owes the reader is the page a tap
  opens, not the bytes that carried it there, so two jobs run in order and stay
  apart. First the address is written the way the Markdown consumer on the
  other side writes it. That rule is `core/reply_enhancer.py`'s `spell_uri`,
  promoted out of `core/citations.py` where it already existed as the
  renderer-compatible half of `_canonical_uri`: `<`, `>` and `|` leave as
  `%3C`, `%3E` and `%7C`, along with spaces, control characters, backslashes,
  backticks, bracket data and a `%` that starts no escape, while `?#&=` and an
  escape the address already spells are left standing. Then Slack's text-object
  escaping runs over the result, which is what a query separator needs:
  `?a=1&b=2` travels as `?a=1&amp;b=2` and is `?a=1&b=2` again to the client,
  where percent-encoding that `&` would have survived every decoder and opened
  a different page.

  Picking a different safe set is not a free choice about wire bytes. A bare
  `%` and a bracket in a path survive URL parsing as distinct spellings, so
  delivering `…/a%b` where the renderer resolves `…/a%25b` is a different path
  on any server we do not control — measured, not assumed, by rendering both
  consumers and comparing `URL.href` over a bounded matrix
  (`tests/fixtures/link_destination_matrix.json`, asserted from both sides).
  The one escape that has to come back is the pair of brackets around an IPv6
  host: they are the authority's syntax rather than data, and nothing opens
  `%5B::1%5D`. But only for a host that was *written* in brackets, and the
  spelled URL can no longer answer that: `[` and `]` are outside the URI-safe
  set, so spelling writes a literal `[::1]` and a provider's own `%5B::1%5D`
  as the same characters. Repairing whatever looks escaped turned
  `https://%5B::1%5D/admin`, which names no host at all, into a live link to
  the loopback interface — and `safe_url`, the Web renderer and Slack all
  agreed on that repair, which is agreement on the bug rather than proof of
  correctness. So the destination is handed over *before* it is spelled:
  `spell_destination` does both halves, qualifying the authority from the
  resolved destination (`_authority_brackets_literal`) and then putting the
  brackets back only there, only when the authority parses, and only when both
  ends of the wrapper were literal — a mixed `[::1%5D` is not a bracketed host
  with an escape in it, it is a host that was never bracketed at all, and
  promoting half of it into syntax invents an address out of the other half.
  The blind form of the call is gone rather than deprecated, so a caller cannot
  silently forget to say which spelling it started from. A malformed-looking
  `%zz` is still left alone, because that is what the consumer's own rule does
  with it rather than a policy chosen here.
- **A backslash escape is resolved before a platform reads it.** An escape says
  one character is not syntax, and no IM dialect knows that. Telegram and Slack
  re-read the escaped character as markup of their own — Slack's converter
  turns an escaped `*` into `_`, so the reader is shown a different character
  than the writer wrote — and WeChat, whose renderer is a sequence of regex
  passes, both shows the backslash and lets an escaped `]` close a link label
  early. So a shared helper in the formatter layer resolves the escapes and
  holds what they protected behind a placeholder until the platform pass is
  done, and each of those three platforms applies it — honouring upstream's
  intent in the dialect that is actually about to parse the text. Discord and
  Feishu are left as they were: their `format_markdown` returns the text
  unchanged, so nothing here re-reads it. A destination is held the same way and
  for a stronger reason: Slack's converter scans the whole line for emphasis,
  including inside the parentheses, and turned `https://a*b*.example/x` into
  `https://a_b_.example/x` — not a formatting difference but a different site.
  A destination is opaque data to a text dialect, so it is held across the
  platform pass and comes back untouched by it — and, on a platform whose own
  wrapper it then has to fit inside, is re-spelled for that wrapper while the
  wrapper is still open, never restored into a finished one. Labels are escaped for
  every character that is active syntax in a dialect the label will be read in:
  the ones that destroy the link around it (a backtick or an angle bracket opens
  a code span, comment, processing instruction, declaration or CDATA section
  that runs past `](url)` and leaves the reader raw Markdown with nothing to
  click, and a bracket or backslash ends the label early), and the ones that
  merely re-read it as markup — `*`, `_`, `~`, `&`, `|`. The second group used
  to be left alone because the citation's identity was read back off its
  rendered text, so escaping it cost the badge; identity is registered before
  any of this runs now, so the label can be protected at no such price. It has to
  be: a host is the attribution, and `a*b*.example` shown as `ab.example` in
  italics names a site the link does not open.
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
  its controls, private-use characters and the invisible formatting characters
  that could render its own text backwards — the bidi marks, embeddings,
  overrides and isolates — stripped, while the joiners and variation selectors
  a real title needs to spell a family emoji, a Persian word or a text-style
  symbol are kept, and it is collapsed and bounded,
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
  preserved, lone surrogates repaired to `U+FFFD`, and the result a fixed point
  of itself —
  and the scheme lowercased, since a renderer that only knows the lowercase
  spelling sees no link at all (Telegram delivered
  `[example.com](HTTPS://Example.com/X)` as raw Markdown). Parentheses are
  part of the address and stay literal in the URL and the sidecar: encoding
  them as `%28`/`%29` protected the link and named a different page. Only the
  Markdown link guards them, as `\(`/`\)` in the destination, which the Web
  renderer, Slack, Telegram and WeChat each resolve back to the character. An
  angle-bracket destination `<url>` says the same in CommonMark, but the real
  Telegram and WeChat paths do not read it as a link, so it is not used. A raw
  backslash left after normalization — literal or spelled `&#92;`/`&bsol;` — is
  refused: WHATWG reads it as `/` in an http(s) authority and path and as data
  in a query, so no single spelling of it names the page for every reader
  (`https://trusted.example\@attacker.example/x` opens trusted.example and,
  percent-encoded, was attributed to attacker.example). An already-encoded
  `%5C` is data everywhere and is kept. Beyond that the URL
  is left as it arrived: it names the page its source gave, and `:0080`,
  `Example.COM` and an uncompressed IPv6 address all open the same page, so this
  is not the place to decide two spellings are one. What is judged is only
  whether a browser opens it at all — one authority judgment covering userinfo
  (split at the **last** `@`, never case-folded, it may be a password), a
  bracketed IPv6 literal (WHATWG's own parser, which rejects the zone id
  `ipaddress` accepts), and a port (ASCII digits only, since `int()` reads `٣`
  as 3 where a browser reads no port at all; empty is no port, in range is kept
  as written). The brackets of an IPv6 host are written literally rather than
  percent-encoded, because `%5B` is a destination the URL parser refuses
  outright — and a destination that arrived already spelled that way names no
  host, so it is rejected rather than repaired into one. Valid literal IPv6
  stays supported, userinfo and port included; nothing here is a
  loopback or private-network blacklist, which would reject addresses that are
  perfectly real. The renderer's own href is where that promise was still being
  broken: `mdast-util-to-hast` percent-encodes the brackets on the way out, so
  the anchor for a real IPv6 host rendered correctly and went nowhere. The
  shared `urlTransform` puts them back, in the authority only and only when the
  platform's own URL parser then accepts the result, so the ordinary link
  reaches the same address the badge does — and only for a destination the
  Markdown AST says was written with literal brackets. `remarkLiteralAuthority`
  asks that of `node.url`, or of the definition a reference names, where the
  parsed destination still exists, and marks the node; the mark travels with
  the occurrence rather than with the URL text, so two links in one body that
  spell the same href may disagree about it. It runs on every body, citation or
  not, over inline links, reference links, autolinks and image destinations —
  the component turns an image it will not fetch into a click-through link, so
  an image `src` becomes an `href` too. A generic link a provider wrote encoded
  keeps its invalid spelling and acquires no citation-only policy: preserving
  an invalid address is this renderer's job, minting a valid one is not. A
  badge is a presentation enhancement; it may never be the only thing able to
  navigate. An authority is read only
  where one is written: a browser repairs `https:example.com/x`, but the rule
  that keeps a hostless URL out keeps this out too, and a search result always
  writes the slashes. The label attributes the host a browser would actually reach:
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
  proved it), the URL identity table including the IDN mapping cases and the
  authority table (IPv6 literals, ports, userinfo), which links each citation
  wrote and which prose links it did not, the real `WeChatBot.format_markdown`
  boundary rather than its formatter alone, message readiness at
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
- `tests/test_markdown_escape_delivery.py` — the same escaped text through
  Telegram, Slack and WeChat: the reader sees the character and no backslash,
  an escape inside a code span stays a backslash, and an escaped backtick in a
  label no longer takes the link and the code span after it.
- `ui/src/components/ui/markdown.test.tsx` — renderer matching and provenance:
  a word-for-word prose link left alone, a badge on each link one source wrote,
  a body that is not the one the ranges were measured in degrading to plain
  links, a range covering only part of a link matching nothing, the same exact
  spelling inside a code span, a fenced block, a footnote definition, a table
  cell or an image alt moving nothing, an image / autolink / reference link
  spelling the same page never badged, a legacy row still recognized, plus
  hover/focus/touch behavior and accessible naming.
- `tests/fixtures/citation_authority_matrix.json` — one table for the brackets
  an authority may keep, read by three separate surfaces so that none of them is
  the oracle for another: `tests/test_citations.py` asserts `safe_url` of the
  destination (a rejection recorded as `null`, not omitted because constructing
  the URL throws), `ui/src/components/ui/markdown.test.tsx` renders the same
  Markdown through the real component and asserts the anchor's exact `href` and
  then the address a browser resolves from it — or that a browser refuses it —
  and `tests/test_link_unit_delivery.py` decodes the destination back out of the
  real Slack wrapper. Agreement between the three on repairing an encoded
  wrapper would be agreement on the bug, which is why the columns are measured
  rather than derived. The rows cover literal brackets, an upper- and a
  lowercase encoded wrapper, each mixed wrapper, a double-encoded one, userinfo
  and an empty userinfo, an in-range and an out-of-range port, a malformed IPv6
  host, bracket data in path, query and fragment, and the escape and
  character-reference spellings that resolve to literal brackets. One row is two
  links in the SAME body whose hrefs are identical after naive spelling, which is
  what proves the qualification is kept per occurrence rather than per URL.
- `tests/test_citations.py::TestWhereACitationCanBeShown` — the producer half of
  where a citation may land: a source written after the unit for a marker at the
  start, middle and end of a label, in an image's alt text, inside an image
  inside a link, in an explicit full-reference label, and after non-ASCII text
  whose spans are counted in UTF-16; two markers in two labels as two spans of
  one source in reading order; and, byte for byte unchanged with an empty
  sidecar and an empty `citation_ref_ids`, a marker in a destination, a title,
  an angle destination, an angle autolink, an image `src`, a reference
  identifier and its definition, a shortcut and a collapsed reference whose
  definition repeats the marker, and the four forms whose definition does not —
  shortcut and collapsed, link and image — where the marker alone is what stops
  the run resolving. Then
  the tokens a transform moved, judged where they ended up, and the structured
  fields — a file label and a quick-reply button — keeping plain attribution in
  place. `TestRegisteredIdentity` already covered the copied, deleted, spliced
  and moved-into-code tokens and is unchanged.
- `ui/src/lib/citations.test.ts` — the digest and the remap in isolation: the
  UTF-16LE digest against CJK, a non-BMP emoji, CRLF and an unpaired surrogate;
  and `remapCitations` over deletions, insertions and length changes before,
  after and across a span, where crossing one invalidates that citation.
  `ui/src/lib/mentions.test.ts` does the same for the edits the mention pass
  reports. `tests/fixtures/citation_body_digest.json` is the one table both
  ends of the digest are asserted against.
- `tests/test_citation_exits.py` — the exit census. One real producer run is
  driven through the real `ConsolidatedMessageDispatcher` against a temporary
  SQLite home, per surface and per level (delivered result, silent result,
  intermediate, notify, suppressed delivery, Harness terminal, tool trace,
  SSE), and every copy that leaves is checked: no internal token at any exit,
  the attribution present in each, and three private-use literals the product
  must NOT touch — a code-fenced marker, a truncated marker, and an unrelated
  loose `U+F8FF` — surviving verbatim.
- `tests/test_link_unit_delivery.py` — the whole-link hold, on the consumers
  that would swallow it: a footnote definition, a table row, an HTML block,
  ordinary prose and prose wrapped across lines, plus a code span written
  across two lines that must stay one code span. Then what happens to the unit
  after the hold: a label spelling `&`, `<` or `>` — written plainly, written
  as a backslash escape restored after the converter ran, or looking like a
  mention. What Slack shows is then
  asserted against CommonMark itself: each named and numeric reference is
  paired with its escaped spelling (`&amp;`/`\&amp;`, `&lt;`/`\&lt;`,
  `&gt;`, `&copy;`/`\&copy;`, `&#38;`, `&COPY;`), a real `markdown-it` render
  says what a reader should see, and Slack's wire form is decoded back and
  required to equal it — with a reference inside a code literal left written
  out and a nested-looking one decoded only once. Two producer-side citations
  of `https://a%26amp%3B.example/x` and `https://a%26lt%3B.example/x` run the
  same assertion end to end, over both the visible label and the destination.
  The oracle is asked for every name the parser knows — the whole entity table
  — and for a bounded numeric matrix: decimal and hexadecimal widths on either
  side of the grammar's limit, code points that are valid, recognized but
  unencodable, or past the last one, and each of those escaped, nested-looking,
  or inside a code literal. A label is then required to be one line however its
  break is spelled: a source LF or CRLF, `&#10;`, `&#xA;`, `&NewLine;`,
  `&#13;`, and `\&NewLine;` staying literal, with the address left alone.
  The destination has a corpus of its own, run as one batch through the real
  adapter: each wrapper delimiter written raw, backslash-escaped and as a
  character reference; a reference spelling a reference and the escape that
  spells the same five characters; references that produce a control character
  or a space; `%xx` that was already spelled and a `%b` that is not an escape
  at all; a query, a fragment, a bracketed IPv6 authority with userinfo and
  port, a non-ASCII host and path, brackets, a backslash, backticks, and a
  `mailto:` address. Every row asserts the exact wire and, separately, the
  address that wire resolves to — the wrapper is parsed structurally first,
  before a single pass of Slack's three decodings over each field, so a
  permissive decoder cannot make a delimiter that ended the unit early look
  like a working link, and the count of units is part of the assertion. An
  empty label exercises the bare `<url>` branch and two adjacent links the
  composition. The same corpus is rendered by the Web `Markdown` component in
  `ui/src/components/ui/markdown.test.tsx`, whose anchors are the independent
  answer for what address each link names; the two agree on every row except
  the three the adapter deliberately leaves alone.
  `ui/src/components/ui/markdown.test.tsx` asks the real Web renderer the same
  eight reference questions, so the two surfaces are measured against one
  answer rather than against each other's expectations.
  Then the boundary: a fitting link crossing a proposed boundary, a fitting
  link at the head of a chunk, two adjacent links, and a multibyte byte budget,
  each keeping every link whole and every chunk within the platform limit —
  and a link scan made to fail, which must not certify the plan it could not
  read. Then the delivery itself, through the real dispatcher with only the
  network replaced: a text-only client sending a fitting link as ordinary text,
  with no attachment and no delivery-failure notice; Discord attaching the
  complete result rather than fragmenting a link no message can hold; the real
  WeChat adapter carrying it over `upload_file_from_path` with the staging file
  cleaned up; a refused CDN upload reported as no delivery rather than as an
  attachment; and the same three answers on the intermediate log path, driven
  from a registered citation through the real WeChat adapter — in every
  fallback case not one fragment sent before it.
- `ui/src/components/workbench/CitationStoredRows.test.tsx` — the rows
  `tests/citation_bridge.py` wrote through the real dispatcher, read by the
  real `MessageRow` and the real activity card: an IM row whose footer is
  folded into the stored body, the same row without one, a workbench row that
  stores its footer apart, a narration row, and the combination row where the
  backend rewrote an attachment before measuring and the reader then edits the
  body twice more (footer strip, secure-input card) above the citation.
- `tests/citation_bridge.py` → `tests/fixtures/citation_consumer_bridge.json` —
  one recording of what the real producer and the real delivery pass emit for
  each case, so the two ends of the contract are measured against one run
  instead of against each other's assumptions. Nothing in it is hand-authored:
  the body, the sidecar and the links a reader must end up with are derived from
  the run, and regenerating it (`python -m tests.citation_bridge`) is how a
  deliberate change is recorded — including the counterexamples that decide
  where identity comes from: a `<silent>` strip splicing two halves into a
  marker, and a `file://` flattening plus a `<silent>` strip cancelling out so
  that the IM body gains a complete marker the workbench body never forms. Both
  stay literal text, and the sidecar claims the one occurrence the model wrote;
  the test runs a resolve over the delivered body to show what a stage placed
  after delivery would have produced instead. The recording also carries the
  rows the dispatcher stored, read back out of SQLite, which is what the Web
  consumers are handed. `tests/test_citation_consumers.py` asks the
  real Slack, Telegram and WeChat renderers what they show of that recording —
  and Discord and Feishu that they still pass it through — asserting the label
  read and the address reached rather than that a URL appears somewhere; no
  message is sent to any platform. It also drives the real dispatcher with real
  persistence, so the stored row it checks is the one SQLite returns, including
  the `file://` rewrite that runs after delivery and before the sidecar is
  measured. `ui/src/components/ui/citation-bridge.test.tsx`
  asks the real `Markdown` component the same questions, with the sidecar,
  without it, with a stale one and with a pre-provenance one, and again with the
  mention and secret-request rewrites running beside it — those edit the source
  text before Markdown parses it, which is exactly what a positional contract
  can be broken by. Nine cases were added for where a citation may be written:
  a marker in a label and at three positions in one, the data and syntax slots,
  the reference slots including the dual-use shortcut and collapsed forms, an
  image's alt and `src`, an image inside a link, three real `<silent>` strips
  that move a token into a label, a destination and a title, an open fence, and
  a file label beside quick-reply buttons. The recording carries two separate
  questions about its anchors, because a case may answer yes to one and no to
  the other: whether they are every address a reader is given (so a link
  pointing anywhere else is a link nobody wrote), and whether they are also in
  the order shown. An image, an autolink or a reference definition names an
  address the delivery pass's inline-link scan cannot resolve, so those cases
  record the first as false and are asked only which links must appear; every
  case that was recorded before this is unchanged, including the GFM footnote
  that relocates its link.
- `ui/e2e/citations/` — real-browser layout and pointer routing on desktop,
  mobile Chromium, and mobile WebKit, in English and Chinese, including the
  falsifiable form of the no-fetch rule: zero requests to the cited site after
  render and preview, exactly one after an explicit open, and a prose link to a
  cited page that stays an ordinary anchor in a real browser.

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
- **A badge is a Web affordance; IM gets the link.** Every IM surface delivers
  the ordinary Markdown link the rewrite wrote, and the sidecar never travels
  with it. The attribution a reader on IM has is the label and the address, and
  that is the whole contract there.
- **The whole-link hold does not re-open Slack's other seams.** Held units are
  inline links; a link written inside a cross-line inline code span, and an
  image, keep exactly the behaviour they had before — including Slack's
  pre-existing mangling of the former. Reading those lines individually would
  find a "link" no reader is shown, which is the worse failure, and fixing
  Slack's inline-code converter is not this change.
- **The stored row's re-measurement is asserted on the backend side.** A
  `file://` attachment is rewritten to a media-proxy URL inside the same
  transaction that writes the row, so the stored body is not the delivered one
  and its spans and digest are measured after that rewrite. The proxy id is
  minted per registration, so the recorded fixture cannot pin it: the recording
  blanks that one id (and the digest that covers it) and the rebinding itself is
  asserted in Python, where the same run can compare the delivered measurement
  against the stored one.
- **An attachment is a link, so the unit parser had to be told so.**
  `markdown-it`'s default `validateLink` rejects `file:` outright, so the
  CommonMark instance that locates units could not see an attachment at all —
  and a citation placed relative to "the enclosing unit" would have been written
  *inside* an attachment's label, which is the defect this whole rule exists to
  prevent. The unit parsers now use `_validate_file_link_locally`, the
  acceptance rule the reply parser already applied to the same links, rather
  than a second policy about schemes.
- **A token moved into code is asserted directly, not through a delivery
  case.** The transforms that actually run between registration and
  materialization — the `<silent>` strip, the `file://` flattening — can move a
  token into a label, a destination or a title, and each of those is a recorded
  bridge case. None of them can wrap a token in a code span or a fence, so that
  branch of the re-evaluation is asserted where it is reachable, by calling
  materialization with the token already there (`TestRegisteredIdentity`). It is
  not claimed as delivery-path coverage.
- **Two product calls are covered by argument rather than by a test.** The
  dispatcher materializes once more inside the duplicate-result short-circuit,
  where `_accepted_message_result_text` only reads the fallback text when the
  accepted message is not a mapping — reachable with no token in it. It is kept
  because removing it would leave a shape that leaks a token the moment that
  branch changes. On the Web side, `resultFooterParts` emits only trailing cuts
  for every input a stored row can have, so the remap through its edits is
  either a no-op or a whole-body invalidation there; the remap itself is
  asserted directly in `ui/src/lib/citations.test.ts`. Neither is claimed as
  census coverage.
