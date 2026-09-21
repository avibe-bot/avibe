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
// not to any link the answer's own prose happens to point at the same page. So
// the sidecar records where each link it wrote sits among the links sharing that
// destination, and `remarkCitationOccurrences` counts the same thing here from
// the delivered text. The destination is the counting key rather than the
// rendered label, because that is what both parsers agree on: a label is what is
// left after emphasis, character references and escapes have been resolved, and
// every one of those is a way for the two to disagree.

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
  /** 1-based positions of the links this citation wrote, among the links
   *  sharing `url`. Absent on rows persisted before provenance existed. */
  occurrences?: number[];
  /** How many links the backend counted for `url` in the whole message. */
  occurrence_total?: number;
};

/** Where one rendered link sits among the links sharing its destination. */
export type CitationOccurrence = {
  /** The parsed Markdown destination — NOT the href the renderer resolved. */
  url: string;
  /** 1-based position in document order among the links sharing `url`. */
  ordinal: number;
  /** How many links this render counted for `url`. */
  total: number;
};

// The annotation travels from the remark plugin to the renderer as hast data
// attributes, which is the one channel mdast → hast preserves verbatim.
const OCCURRENCE_URL = 'dataCitationUrl';
const OCCURRENCE_ORDINAL = 'dataCitationOrdinal';
const OCCURRENCE_TOTAL = 'dataCitationTotal';

/** The parts of an mdast node this counts on, none of which it can assume. */
type MarkdownNode = {
  type?: unknown;
  url?: unknown;
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
 * Annotate each inline link with its position among the links sharing its
 * destination, so `findCitation` can recognize the ones the backend wrote.
 *
 * Only a link actually spelled ``[label](destination)`` is counted, because
 * that is the only shape the backend counts: the first source character is
 * ``[`` and the last is ``)``, and that pair excludes images, reference links
 * and both autolink forms without depending on either parser's vocabulary for
 * them. The key is the parsed destination, never the resolved href — the
 * renderer percent-encodes an href, so an IPv6 literal's brackets would stop
 * being the URL the backend stored.
 */
export function remarkCitationOccurrences() {
  return (tree: unknown, file: { value?: unknown }) => {
    const source = typeof file?.value === 'string' ? file.value : '';
    if (!source) return;
    const totals = new Map<string, number>();
    const counted: Array<{ link: MarkdownNode; url: string; ordinal: number }> = [];
    eachLink(tree as MarkdownNode, (link) => {
      const start = link.position?.start?.offset;
      const end = link.position?.end?.offset;
      const url = link.url;
      if (typeof start !== 'number' || typeof end !== 'number') return;
      if (typeof url !== 'string' || !url) return;
      if (source[start] !== '[' || source[end - 1] !== ')') return;
      const ordinal = (totals.get(url) ?? 0) + 1;
      totals.set(url, ordinal);
      counted.push({ link, url, ordinal });
    });
    // The total is only known once the walk ends, so the writes wait for it.
    for (const { link, url, ordinal } of counted) {
      const data = (link.data ??= {});
      const properties = (data.hProperties ??= {});
      properties[OCCURRENCE_URL] = url;
      properties[OCCURRENCE_ORDINAL] = ordinal;
      properties[OCCURRENCE_TOTAL] = totals.get(url);
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
  const url = properties[OCCURRENCE_URL];
  const ordinal = properties[OCCURRENCE_ORDINAL];
  const total = properties[OCCURRENCE_TOTAL];
  if (typeof url !== 'string' || typeof ordinal !== 'number' || typeof total !== 'number') {
    return null;
  }
  return { url, ordinal, total };
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
    if (Array.isArray(candidate.occurrences)) {
      // A row that records its provenance is matched on provenance alone. An
      // ordinal it does not list belongs to the answer's own prose, and a total
      // this render disagrees with means the two sides are reading different
      // text — where the honest answer is the plain link the reader already has,
      // not a badge on whichever link happened to land in that position.
      if (
        occurrence
        && candidate.url === occurrence.url
        && candidate.occurrence_total === occurrence.total
        && candidate.occurrences.includes(occurrence.ordinal)
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
