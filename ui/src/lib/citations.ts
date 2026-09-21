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
// backend knows exactly which characters it wrote and where, so that is what
// travels: the complete link spelling, plus which of that spelling's literal
// occurrences in the delivered text are its own.
//
// Counting a literal substring is the one measurement two Markdown
// implementations cannot disagree about. Counting parsed links did disagree: a
// GFM footnote definition holding `[p](url)` is a link to this renderer and part
// of a definition to the backend, and the real citation silently lost its badge
// over the difference. So both sides now count the same characters in the same
// text, and a link is only upgraded when its source span IS one of the
// occurrences the backend claims.

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
  /** The complete Markdown link the backend wrote, character for character —
   *  `[escaped label](url)`. Present only on rows written with provenance;
   *  its absence is what selects the legacy match below. */
  spelling?: string;
  /** 1-based positions of the links this citation wrote, among the literal
   *  occurrences of `spelling`. Absent on rows persisted before provenance. */
  occurrences?: number[];
  /** How many occurrences of `spelling` the backend counted in the message. */
  occurrence_total?: number;
};

/** Where one rendered link sits among the literal occurrences of its spelling. */
export type CitationOccurrence = {
  /** The exact source characters this link is spelled with. */
  spelling: string;
  /** 1-based position among the literal occurrences of `spelling`. */
  ordinal: number;
  /** How many occurrences of `spelling` this render counted. */
  total: number;
};

// The annotation travels from the remark plugin to the renderer as hast data
// attributes, which is the one channel mdast → hast preserves verbatim.
const OCCURRENCE_SPELLING = 'dataCitationSpelling';
const OCCURRENCE_ORDINAL = 'dataCitationOrdinal';
const OCCURRENCE_TOTAL = 'dataCitationTotal';

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
 * Every offset in `source` where `needle` occurs, in order.
 *
 * The next search starts one character on rather than one needle on, so an
 * overlapping occurrence still counts. A link spelling cannot overlap itself —
 * it opens with `[` and closes with `)` — but the backend scans the same way
 * without proving that either, and the two scans have to agree exactly.
 */
function literalOccurrences(source: string, needle: string): number[] {
  const found: number[] = [];
  for (let at = source.indexOf(needle); at !== -1; at = source.indexOf(needle, at + 1)) {
    found.push(at);
  }
  return found;
}

/**
 * Annotate each inline link with the source characters it is spelled with and
 * which of that spelling's literal occurrences it is, so `findCitation` can
 * recognize the links the backend wrote.
 *
 * The spelling is the exact source slice, not the parsed destination and not
 * the rendered label: both of those are what is left after escapes, character
 * references and emphasis have been resolved, and every one of those is a way
 * for two implementations to disagree. Every literal occurrence counts, whether
 * this renderer reads it as a link, a code example or a footnote — the backend
 * counted it too, and a shared count is the whole point.
 */
export function remarkCitationOccurrences() {
  return (tree: unknown, file: { value?: unknown }) => {
    const source = typeof file?.value === 'string' ? file.value : '';
    if (!source) return;
    const positions = new Map<string, number[]>();
    const annotate: Array<{ link: MarkdownNode; spelling: string; ordinal: number }> = [];
    eachLink(tree as MarkdownNode, (link) => {
      const start = link.position?.start?.offset;
      const end = link.position?.end?.offset;
      if (typeof start !== 'number' || typeof end !== 'number') return;
      const spelling = source.slice(start, end);
      if (!spelling) return;
      let occurrences = positions.get(spelling);
      if (!occurrences) {
        occurrences = literalOccurrences(source, spelling);
        positions.set(spelling, occurrences);
      }
      // Only a link whose source span coincides exactly with an occurrence may
      // be upgraded; the slice was taken from that span, so this holds unless
      // the node's own offsets disagree with the source it came from.
      const ordinal = occurrences.indexOf(start) + 1;
      if (!ordinal) return;
      annotate.push({ link, spelling, ordinal });
    });
    for (const { link, spelling, ordinal } of annotate) {
      const data = (link.data ??= {});
      const properties = (data.hProperties ??= {});
      properties[OCCURRENCE_SPELLING] = spelling;
      properties[OCCURRENCE_ORDINAL] = ordinal;
      properties[OCCURRENCE_TOTAL] = positions.get(spelling)?.length;
    }
  };
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

/** The annotation `remarkCitationOccurrences` left on the rendered link. */
function linkOccurrence(node: unknown): CitationOccurrence | null {
  const properties = (node as { properties?: Record<string, unknown> } | undefined)?.properties;
  if (!properties) return null;
  const spelling = properties[OCCURRENCE_SPELLING];
  const ordinal = properties[OCCURRENCE_ORDINAL];
  const total = properties[OCCURRENCE_TOTAL];
  if (typeof spelling !== 'string' || typeof ordinal !== 'number' || typeof total !== 'number') {
    return null;
  }
  return { spelling, ordinal, total };
}

/**
 * The citation a rendered Markdown link stands for, or null for an ordinary link.
 *
 * `node` is the hast element the renderer was handed; `href` and `text` are the
 * destination it resolved and the link's visible text.
 */
export function findCitation(
  citations: CitationSource[] | undefined,
  node: unknown,
  href: string,
  text: string,
): CitationSource | null {
  if (!citations?.length) return null;
  const occurrence = linkOccurrence(node);
  for (const candidate of citations) {
    if (!isUsable(candidate)) continue;
    if (candidate.spelling) {
      // A row that carries its provenance is matched on provenance alone. An
      // ordinal it does not list belongs to the answer's own prose, and a total
      // this render disagrees with means the two sides are reading different
      // text — where the honest answer is the plain link the reader already has,
      // not a badge on whichever link happened to land in that position.
      if (
        occurrence
        && candidate.spelling === occurrence.spelling
        && candidate.occurrence_total === occurrence.total
        && candidate.occurrences?.includes(occurrence.ordinal)
      ) {
        return candidate;
      }
      continue;
    }
    // A row persisted before provenance existed still has to render, so it keeps
    // the match it was written for: the exact (destination, link text) pair.
    if (href && text && candidate.url === href && candidate.label === text) return candidate;
  }
  return null;
}
