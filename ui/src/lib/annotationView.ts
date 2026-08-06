// What a Show Page annotation draws in chat, distilled from ``content.annotation``.
// Design: design.pen m31JWV (states) + TxFKk (anatomy and rules); the rule
// numbers cited below are that frame's.
//
// Pure, and separate from the card component, so the transcript's branch mapper
// can depend on it without a lib → component import.

export type AnnotationView = {
  // Rule 01: direction alone decides the side and the title. A forward
  // annotation is authored by ``harness`` (it is turn input), so reading
  // ``author`` here would put the user's own annotation on the left behind a
  // harness chip. ``author`` is bookkeeping and never reaches the view.
  direction: 'user' | 'agent';
  // Rule 07: only ``resolved`` draws a marker; created / updated / dismissed
  // draw nothing beyond the body. Collapsing the action to a boolean means no
  // later edit can quietly start branching on the other three without going
  // back to the design first.
  resolved: boolean;
  quote?: string;
};

// Reads ``content.annotation`` off a chat row; null when the row carries no
// usable display record.
export function readAnnotationView(content: unknown): AnnotationView | null {
  const raw = (content as { annotation?: unknown } | null | undefined)?.annotation;
  if (!raw || typeof raw !== 'object') return null;
  const { direction, action, quote } = raw as Record<string, unknown>;
  if (direction !== 'user' && direction !== 'agent') return null;
  return {
    direction,
    resolved: action === 'resolved',
    // Rule 04: the strip needs copy the reader can find on the page. An empty
    // string is the same as no quote.
    quote: typeof quote === 'string' && quote.trim().length > 0 ? quote : undefined,
  };
}

// Rule 02: exactly two title values, and the action never enters the title — a
// resolved agent mark is still titled "Agent 批注"; the marker below the body is
// what says it is done. The direction is data on the row; the words are frontend
// i18n, so switching UI language re-labels existing rows instead of rewriting them.
export const annotationTitleKey = (direction: AnnotationView['direction']): string =>
  direction === 'user' ? 'chat.annotation.titleUser' : 'chat.annotation.titleAgent';

// What names a queued annotation on the strip's single line when its author
// wrote no words.
//
// A queued annotation is visible in the strip and nowhere else — its type is
// ``queued``, so the transcript cannot draw it — which makes that one line the
// only account of what is waiting. Authored text is the whole line for an
// ordinary queued prompt, but an annotation is allowed to carry none: a pure
// highlight contributes its quote, a boxed region contributes its screenshot.
// Left to the text alone, those two draw a title and then nothing.
//
// The card answers the same question by stacking quote and attachments under
// the title. This picks whichever of them fits one line, in the order the card
// stacks them, so the strip and the bubble that replaces it say the same thing.
export type AnnotationStandIn = { kind: 'quote'; quote: string } | { kind: 'screenshot' } | null;

export function annotationStandIn(
  view: AnnotationView,
  text: string | null | undefined,
  hasAttachments: boolean,
): AnnotationStandIn {
  // The annotator's own words always speak for the row; a stand-in only ever
  // fills a silence.
  if (text && text.trim().length > 0) return null;
  if (view.quote) return { kind: 'quote', quote: view.quote };
  // Rule 05 in reverse: on the card a locator is never shown because the reader
  // cannot use it, and the same holds here — an anchor with no quote and no
  // region leaves the title to stand alone rather than printing a selector.
  return hasAttachments ? { kind: 'screenshot' } : null;
}
