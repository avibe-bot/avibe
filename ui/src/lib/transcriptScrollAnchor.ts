// The chat transcript keeps its own scroll-anchor: while the reader is scrolled
// up in history, it remembers an element in view and where its top sits inside
// the scrolled content, so any later change to the content above it can be
// compensated by exactly that much (iOS Safari before 27 ships no CSS
// ``overflow-anchor``, and the container opts out of the native one so every
// browser behaves alike).
//
// Positions are taken inside the content, not against the viewport: the
// reader's own scrolling never moves an element within the content, so it can
// never be mistaken for a shift and written back. A viewport position goes
// stale the moment the reader scrolls again, and with asynchronous scrolling
// (iOS, and WebKit generally) a commit can land after the scroll position moved
// but before that move's scroll event re-captured the anchor.
//
// The anchor is only as good as the element it picks, and the property it must
// have is positional: **it has to move when an older page is prepended.** Older
// messages are inserted at the head of ``messages``, so everything the transcript
// renders ABOVE that list — the fork-source banner, the loading / end-of-history
// slot, the null-anchor activity chips — stays exactly where it is across the
// load. Anchoring to one of those restores nothing: the delta is zero, so the
// viewport keeps the raw top-of-window ``scrollTop`` and the reader is dropped on
// the oldest row of the page that just arrived.
//
// Restore after the message DOM commits, even when trimming the retained window
// cancels out the prepended height. A ResizeObserver alone cannot detect that
// change. Only the reader's next upward input asks for another page.
//
// So the rule below is structural rather than a list of elements to avoid: the
// search starts at the first message row and never looks above it. Chrome added
// above the transcript later is excluded by construction instead of by someone
// remembering to mark it.
//
// Among the rows from there on, the anchor is the first one whose top is inside
// the viewport. The row straddling the viewport top is the anchor only when no
// row starts below it on screen (one row taller than the viewport): an image
// that loads in the part of that row above the edge would otherwise grow it
// under the reader without anything below it being compensated.
//
// A row is a message row (``data-message-id``) or a row standing for several of
// them (``data-transcript-row``): a collapsed Harness group is one element for
// many messages, and it moves with a prepend exactly as the rows it holds would.
//
// The anchor also names the message row it holds (``rowId``, ``rowTop``), because
// a message can change element without leaving the transcript: a Harness row
// that an older page turns into a group member is re-mounted inside the group,
// or folded into its collapsed head. ``resolveScrollAnchor`` follows it there.
import { placeIn } from './layoutBox';

export type ScrollAnchor = { el: HTMLElement; top: number; rowId?: string; rowTop?: number; key?: string };

/** The visible band of the scroll container, in viewport coordinates. */
export type ScrollViewport = { top: number; bottom: number };

const isTranscriptRow = (el: HTMLElement): boolean =>
  el.dataset.messageId !== undefined || el.dataset.transcriptRow !== undefined;

// A row with an animation of its own running (a reply growing in at the tail,
// ui: transcriptMotion) is drawn away from where its layout puts it until it
// ends. Its box would read the animation as the content moving, so it is never
// the anchor; the rows around it are where they are.
const isAnimating = (el: HTMLElement): boolean =>
  typeof el.getAnimations === 'function' && el.getAnimations().some((animation) => animation.playState === 'running');

/**
 * The anchor among the content's children. ``viewport`` decides which element
 * the reader sees; ``contentTop`` (the content box's top, in the same viewport
 * coordinates) is what the anchor's position is measured from.
 */
export const pickScrollAnchor = (
  children: readonly HTMLElement[],
  viewport: ScrollViewport,
  contentTop: number,
  content?: Element,
): ScrollAnchor | null => {
  // Everything before the first row holds still while the content grows above it,
  // so it can never restore anything.
  const firstRow = children.findIndex(isTranscriptRow);
  if (firstRow < 0) return null;
  let straddling: { el: HTMLElement; rect: DOMRect } | null = null;
  for (let i = firstRow; i < children.length; i += 1) {
    const el = children[i];
    const rect = el.getBoundingClientRect();
    // Entirely above the viewport top — scrolled past, not what the reader sees.
    if (rect.bottom <= viewport.top || isAnimating(el)) continue;
    if (rect.top < viewport.top) {
      straddling ??= { el, rect };
      continue;
    }
    if (rect.top < viewport.bottom || !straddling) return anchorAt(el, rect, contentTop, content);
    break;
  }
  return straddling ? anchorAt(straddling.el, straddling.rect, contentTop, content) : null;
};

/**
 * The anchor for a change the reader makes: the row holding what they touched
 * (a Harness group they open, an activity chip, a long message), so what opens
 * grows from where they touched it. The first row on screen would instead hold
 * still while the opened content is compensated out of view above it. Null for
 * anything above the first row, which holds still anyway.
 */
export const anchorForTouched = (
  children: readonly HTMLElement[],
  target: Node,
  contentTop: number,
  content?: Element,
): ScrollAnchor | null => {
  const firstRow = children.findIndex(isTranscriptRow);
  const touched = children.findIndex((child) => child.contains(target));
  if (firstRow < 0 || touched < firstRow) return null;
  const el = children[touched];
  return anchorAt(el, el.getBoundingClientRect(), contentTop, content);
};

/** Where `el`'s top is inside the content: given the content, where the layout puts it there, less what a
 *  disclosure's motion shows as movement (a row a disclosure above it still pushes down is drawn away from its
 *  layout place, and the motion, not the scroll position, makes up the difference; lib/layoutBox), else where
 *  it is drawn, from `contentTop`. */
export const topInContent = (el: Element, contentTop: number, content?: Element, rect?: DOMRect): number => {
  if (!content || !content.contains(el)) return (rect ?? el.getBoundingClientRect()).top - contentTop;
  let item: Element = el;
  while (item.parentElement && item.parentElement !== content) item = item.parentElement;
  return placeIn(content, el, item);
};

function anchorAt(el: HTMLElement, rect: DOMRect, contentTop: number, content?: Element): ScrollAnchor {
  const anchor: ScrollAnchor = { el, top: topInContent(el, contentTop, content, rect) };
  // A transcript item that is not a message (an activity card or chip, a
  // Harness group's members, its collapse bar) names itself, so it can be found
  // again when it re-mounts.
  if (el.dataset.transcriptKey) anchor.key = el.dataset.transcriptKey;
  const row = el.dataset.messageId !== undefined ? el : el.querySelector<HTMLElement>('[data-message-id]');
  if (row?.dataset.messageId) {
    anchor.rowId = row.dataset.messageId;
    anchor.rowTop = topInContent(row, contentTop, content);
  } else if (el.dataset.harnessMembers) {
    // A group head stands for its members; should the group dissolve, its
    // first message's row takes the head's place.
    anchor.rowId = el.dataset.harnessMembers.split(' ')[0];
    anchor.rowTop = anchor.top;
  }
  return anchor;
}

const byAttribute = (content: ParentNode, selector: string) => content.querySelector<HTMLElement>(selector);

/**
 * The element to restore against, and where its top belongs inside the content.
 * The anchored element while it is still mounted; otherwise the item of the same
 * ``data-transcript-key`` where it was; otherwise the same message's row wherever
 * it now renders; otherwise the collapsed group that folded it in
 * (``data-harness-members`` lists a group's message ids), placed where the row
 * was. ``null`` when nothing of it is left in the transcript.
 */
export const resolveScrollAnchor = (
  anchor: ScrollAnchor,
  content: ParentNode,
): { el: HTMLElement; top: number } | null => {
  if (anchor.el.isConnected) return { el: anchor.el, top: anchor.top };
  // Quoted attribute values need only quotes and backslashes escaped.
  const quoted = (value: string) => value.replace(/["\\]/g, '\\$&');
  if (anchor.key !== undefined) {
    const item = byAttribute(content, `[data-transcript-key="${quoted(anchor.key)}"]`);
    if (item) return { el: item, top: anchor.top };
  }
  if (anchor.rowId === undefined || anchor.rowTop === undefined) return null;
  const id = quoted(anchor.rowId);
  const row =
    byAttribute(content, `[data-message-id="${id}"]`) ??
    byAttribute(content, `[data-harness-members~="${id}"]`);
  return row ? { el: row, top: anchor.rowTop } : null;
};
