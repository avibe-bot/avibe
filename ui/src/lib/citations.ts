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
};

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

/**
 * The citation a rendered Markdown link stands for, or null for an ordinary link.
 *
 * Matched on the exact (destination, link text) pair the backend emitted rather
 * than on the destination alone, so a link the agent wrote itself in its own
 * prose keeps its own wording instead of collapsing into a numbered badge.
 */
export function findCitation(
  citations: CitationSource[] | undefined,
  href: string,
  text: string,
): CitationSource | null {
  if (!citations?.length || !href || !text) return null;
  for (const candidate of citations) {
    if (isUsable(candidate) && candidate.url === href && candidate.label === text) return candidate;
  }
  return null;
}
