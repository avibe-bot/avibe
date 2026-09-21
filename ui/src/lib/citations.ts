// Shared contract for agent source citations.
//
// Some agent backends answer with opaque inline citation markers instead of
// links. The backend resolves each marker against the search results it already
// received and rewrites it into an ordinary Markdown link, plus a structured
// sidecar in `message.content.citations` (see core/citations.py). The delivered
// text is therefore already complete on every surface — an IM platform shows a
// normal clickable link — so the sidecar adds no attribution of its own: it is
// what lets this renderer RECOGNIZE those links and draw them as compact
// numbered badges with a source preview, with nothing re-parsed and nothing
// fetched.
//
// Recognizing one is a provenance question rather than a text question. A badge
// is an attribution claim, so it belongs to a link the backend actually wrote —
// not to any link the answer's own prose happens to point at the same page. The
// backend knows exactly which characters it wrote and where, so what travels is
// where those links SIT: half-open `[start, end)` ranges in UTF-16 code units.
//
// A range alone is not an identity, though. Two ordinary links can be spelled
// the same, and deleting a paragraph moves one of them into the range another
// occupied: with `[example.com](https://example.com/x)` written twice, the
// second link sits at `[38, 74)` and the first at `[0, 36)`, so dropping the
// first paragraph leaves an ordinary link at `[0, 36)` wearing the citation's
// coordinates. So every row also carries `body_sha256`, the digest of the exact
// body its ranges were measured in, and this file verifies that digest BEFORE
// it trusts a single range. The digest says which VERSION of the body is meant;
// it is computed locally on both ends and claims nothing about where the text
// came from.
//
// The body the renderer is finally handed is not always the body the backend
// measured: the transcript strips a generated result footer, and `@<…>` chips
// and `$<NAME>` cards are minted by rewriting the source before Markdown parses
// it. Each of those passes reports what it changed as `TextEdit`s, and the
// ranges are carried across them (`remapCitations`). A pass that edits INTO a
// citation's own range invalidates that citation rather than guessing which
// half of the result the attribution belongs to.

import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex } from '@noble/hashes/utils.js';

export type CitationSource = {
  /** 1-based first-appearance order within the message; the badge's label. */
  index: number;
  /** The backend-native reference id the marker carried (diagnostics only). */
  ref_id?: string;
  /** The cited page's title, as the backend's search returned it. Untrusted. */
  title?: string;
  /** Exactly the destination the backend wrote into the Markdown link. */
  url: string;
  /** Exactly the link text the backend used — the source's domain. */
  label: string;
  /** Half-open `[start, end)` ranges, in UTF-16 code units, of the links this
   *  citation wrote in the body `body_sha256` names. Absent — together with
   *  `body_sha256` — on rows persisted before this contract existed. */
  spans?: number[][];
  /** SHA-256 over that body's UTF-16LE code units, lowercase hex. */
  body_sha256?: string;
};

/**
 * One replacement a pass made to the text it was handed.
 *
 * `start`/`end` are half-open coordinates in that pass's INPUT — not in the
 * original body and not in the pass's output — and `inserted` is how many code
 * units were written in place of the `end - start` that were removed. A
 * deletion inserts 0, an insertion removes 0, and either side may be longer:
 * the two linkify passes are replacements, not insertions, so a length change
 * of any sign has to be carried.
 *
 * Passes compose in the order they ran: each pass's edits are expressed in the
 * output of the pass before it, so `remapCitations` is called once per pass,
 * in that same order.
 */
export type TextEdit = {
  start: number;
  end: number;
  inserted: number;
};

/** The result of a pass that rewrites text and says what it changed. */
export type EditedText = {
  text: string;
  edits: TextEdit[];
};

/** One citation, located in the text a renderer is currently holding. */
export type BoundCitation = {
  source: CitationSource;
  /** Half-open `[start, end)` ranges in that text, in UTF-16 code units. */
  spans: Array<[number, number]>;
};

/**
 * What a renderer needs to draw badges on ONE exact text.
 *
 * `bound` rows are matched positionally and are only meaningful for the text
 * they were last remapped onto. `legacy` rows carry no ranges at all — they
 * were persisted before this contract existed — and keep the match they were
 * written for, the exact (destination, link text) pair.
 */
export type CitationBinding = {
  bound: BoundCitation[];
  legacy: CitationSource[];
};

/** The UTF-16LE bytes of `text`, unpaired surrogates included. */
function utf16leBytes(text: string): Uint8Array {
  const bytes = new Uint8Array(text.length * 2);
  for (let at = 0; at < text.length; at += 1) {
    // `charCodeAt` is the code UNIT, which is what the ranges count and what
    // Python's `surrogatepass` encode writes for an unpaired one.
    const unit = text.charCodeAt(at);
    bytes[at * 2] = unit & 0xff;
    bytes[at * 2 + 1] = unit >>> 8;
  }
  return bytes;
}

/**
 * The digest a sidecar binds its ranges to: SHA-256 over UTF-16LE code units,
 * lowercase hex — byte for byte what `core.citations.body_digest` computes.
 */
export function bodyDigest(text: string): string {
  return bytesToHex(sha256(utf16leBytes(text)));
}

/** Reject anything a stored row could hold that must never become an anchor. */
function isUsable(value: unknown): value is CitationSource {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<CitationSource>;
  if (typeof candidate.url !== 'string' || typeof candidate.label !== 'string') return false;
  if (!Number.isFinite(candidate.index)) return false;
  // The backend already allows only http(s); re-check here because this row came
  // back from storage, and a bad one must degrade to plain text, not to a link.
  return /^https?:\/\//i.test(candidate.url);
}

/** True once a row claims the positional contract, however badly. */
function carriesProvenance(row: CitationSource): boolean {
  return row.spans !== undefined || row.body_sha256 !== undefined;
}

/** The row's ranges, or null if any of them is not one this text can hold. */
function readSpans(raw: unknown, limit: number): Array<[number, number]> | null {
  if (!Array.isArray(raw) || raw.length === 0) return null;
  const spans: Array<[number, number]> = [];
  for (const span of raw) {
    if (!Array.isArray(span) || span.length !== 2) return null;
    const [start, end] = span as unknown[];
    if (!Number.isInteger(start) || !Number.isInteger(end)) return null;
    if ((start as number) < 0 || (end as number) <= (start as number) || (end as number) > limit) {
      return null;
    }
    spans.push([start as number, end as number]);
  }
  return spans;
}

/**
 * Read a stored sidecar against the exact body it was measured in.
 *
 * Returns null when nothing may be drawn, which is the same answer as "no
 * sidecar": the delivered text already carries every link and every label, so
 * refusing costs the reader the preview and nothing else. It is the answer for
 * a digest that does not match, a range outside the body, two ranges that
 * overlap, a row that claims the contract but cannot state it, and a sidecar
 * that mixes rows that claim it with rows that do not — in every one of those
 * the sidecar and the body disagree, and choosing a survivor would be a guess.
 * A failure here never falls back to the legacy match; that match is only for
 * rows that never carried the contract at all.
 */
export function bindCitations(
  citations: readonly unknown[] | undefined,
  body: string,
): CitationBinding | null {
  if (!citations?.length) return null;
  const usable = citations.filter(isUsable);
  if (!usable.length) return null;

  const provenance = usable.filter(carriesProvenance);
  if (!provenance.length) return { bound: [], legacy: usable };
  if (provenance.length !== usable.length) return null;

  const digest = bodyDigest(body);
  const bound: BoundCitation[] = [];
  const claimed: Array<[number, number]> = [];
  for (const source of provenance) {
    if (source.body_sha256 !== digest) return null;
    const spans = readSpans(source.spans, body.length);
    if (!spans) return null;
    bound.push({ source, spans });
    claimed.push(...spans);
  }
  // Each range is one link, so no two of them may share a character.
  claimed.sort((left, right) => left[0] - right[0]);
  for (let at = 1; at < claimed.length; at += 1) {
    if (claimed[at][0] < claimed[at - 1][1]) return null;
  }
  return { bound, legacy: [] };
}

/** Where `[start, end)` lands after `edits`, or null if an edit reached into it. */
function remapSpan(
  start: number,
  end: number,
  ordered: readonly TextEdit[],
): [number, number] | null {
  let shift = 0;
  for (const edit of ordered) {
    // Entirely before: the range moves by however much this edit changed the
    // length. An insertion AT `start` is before the range — it is written where
    // the range begins, so the link still begins just after it.
    if (edit.end <= start) {
      shift += edit.inserted - (edit.end - edit.start);
      continue;
    }
    // Entirely after, and the rest are further right still.
    if (edit.start >= end) break;
    return null;
  }
  return [start + shift, end + shift];
}

/**
 * Carry a binding across one pass's edits, into the text that pass produced.
 *
 * A citation whose range the pass edited into is dropped: the characters the
 * backend pointed at are no longer the characters on screen, and deciding
 * which part of the replacement inherited the attribution would be guessing.
 * `legacy` rows match on text rather than position and are unaffected.
 */
export function remapCitations(
  binding: CitationBinding | null | undefined,
  edits: readonly TextEdit[],
): CitationBinding | null {
  if (!binding) return null;
  if (!edits.length) return binding;
  const ordered = [...edits].sort((left, right) => left.start - right.start);
  const bound: BoundCitation[] = [];
  for (const entry of binding.bound) {
    const moved: Array<[number, number]> = [];
    for (const [start, end] of entry.spans) {
      const next = remapSpan(start, end, ordered);
      if (!next) {
        moved.length = 0;
        break;
      }
      moved.push(next);
    }
    if (moved.length) bound.push({ source: entry.source, spans: moved });
  }
  if (!bound.length && !binding.legacy.length) return null;
  return { bound, legacy: binding.legacy };
}

// The annotation travels from the remark plugin to the renderer as hast data
// attributes, which is the one channel mdast → hast preserves verbatim.
const SPAN_START = 'dataCitationStart';
const SPAN_END = 'dataCitationEnd';

/** The parts of an mdast node this counts on, none of which it can assume. */
type MarkdownNode = {
  type?: unknown;
  position?: { start?: { offset?: unknown }; end?: { offset?: unknown } };
  data?: { hProperties?: Record<string, unknown> };
  children?: unknown;
};

/** Every link node in document order (pre-order is source order for inlines). */
function eachLink(node: MarkdownNode, visit: (link: MarkdownNode) => void): void {
  if (node.type === 'link') visit(node);
  if (!Array.isArray(node.children)) return;
  for (const child of node.children) eachLink(child as MarkdownNode, visit);
}

/**
 * Record where in the source each inline link is written, so `findCitation`
 * can recognize the links the backend wrote.
 *
 * Unist offsets are already UTF-16 code units — a JavaScript string index —
 * which is the unit the sidecar's ranges are counted in. Only an inline link
 * is annotated: a reference link, an autolink and an image spell their
 * destination some other way, and the backend did not write any of them.
 */
export function remarkCitationSpans() {
  return (tree: unknown) => {
    eachLink(tree as MarkdownNode, (link) => {
      const start = link.position?.start?.offset;
      const end = link.position?.end?.offset;
      if (typeof start !== 'number' || typeof end !== 'number') return;
      const data = (link.data ??= {});
      const properties = (data.hProperties ??= {});
      properties[SPAN_START] = start;
      properties[SPAN_END] = end;
    });
  };
}

/** The range `remarkCitationSpans` left on the rendered link. */
function linkSpan(node: unknown): [number, number] | null {
  const properties = (node as { properties?: Record<string, unknown> } | undefined)?.properties;
  if (!properties) return null;
  const start = properties[SPAN_START];
  const end = properties[SPAN_END];
  if (typeof start !== 'number' || typeof end !== 'number') return null;
  return [start, end];
}

/**
 * The citation a rendered Markdown link stands for, or null for an ordinary link.
 *
 * `node` is the hast element the renderer was handed; `href` and `text` are the
 * destination it resolved and the link's visible text, used only by the legacy
 * match. A bound row is recognized by position alone, and the position has to
 * be the WHOLE link: the backend measured the complete `[label](url)` it wrote,
 * so a node covering anything else — a longer link that merely starts there, a
 * fragment of one — simply matches no range.
 */
export function findCitation(
  binding: CitationBinding | null | undefined,
  node: unknown,
  href: string,
  text: string,
): CitationSource | null {
  if (!binding) return null;
  const span = linkSpan(node);
  if (span) {
    for (const entry of binding.bound) {
      for (const [start, end] of entry.spans) {
        if (start === span[0] && end === span[1]) return entry.source;
      }
    }
  }
  // A row persisted before this contract existed still has to render, so it
  // keeps the match it was written for: the exact (destination, link text) pair.
  if (!href || !text) return null;
  for (const candidate of binding.legacy) {
    if (candidate.url === href && candidate.label === text) return candidate;
  }
  return null;
}
