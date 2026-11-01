// The chat transcript keeps its own scroll-anchor: while the reader is scrolled
// up in history, it remembers an element in view and how far its top sits below
// the viewport top, so any later content resize can put that exact element back
// where it was (iOS Safari still ships no CSS ``overflow-anchor``, and the
// container opts out of the native one so every browser behaves alike).
//
// The anchor is only as good as the element it picks, and the property it must
// have is positional: **it has to move when an older page is prepended.** Older
// messages are inserted at the head of ``messages``, so everything the transcript
// renders ABOVE that list — the fork-source banner, the loading / end-of-history
// slot, the null-anchor activity chips — stays exactly where it is across the
// load. Anchoring to one of those restores nothing: the delta is zero, so the
// viewport keeps the raw top-of-window ``scrollTop`` and the reader is dropped on
// the oldest row of the page that just arrived. The transient members of that
// group are worse still — the spinner is unmounted by the very commit that adds
// the page, so the restore is skipped for a disconnected node.
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
// A row is a message row (``data-message-id``) or a row standing for several of
// them (``data-transcript-row``): a collapsed Harness group is one element for
// many messages, and it moves with a prepend exactly as the rows it holds would.
export type ScrollAnchor = { el: HTMLElement; top: number };

const isTranscriptRow = (el: HTMLElement): boolean =>
  el.dataset.messageId !== undefined || el.dataset.transcriptRow !== undefined;

export const pickScrollAnchor = (
  children: readonly HTMLElement[],
  containerTop: number,
): ScrollAnchor | null => {
  // Everything before the first row holds still while the content grows above it,
  // so it can never restore anything.
  const firstRow = children.findIndex(isTranscriptRow);
  if (firstRow < 0) return null;
  for (let i = firstRow; i < children.length; i += 1) {
    const child = children[i];
    const rect = child.getBoundingClientRect();
    // Entirely above the viewport top — scrolled past, not what the reader sees.
    if (rect.bottom <= containerTop) continue;
    return { el: child, top: rect.top - containerTop };
  }
  return null;
};
