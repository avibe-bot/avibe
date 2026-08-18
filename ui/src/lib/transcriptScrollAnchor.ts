// The chat transcript keeps its own scroll-anchor: while the reader is scrolled
// up in history, it remembers the topmost element still in view and how far its
// top sits below the viewport top, so any later content resize can put that exact
// element back where it was (iOS Safari still ships no CSS ``overflow-anchor``,
// and the container opts out of the native one so every browser behaves alike).
//
// The anchor is only as good as the element it picks. Chrome that mounts or
// unmounts AROUND a resize is disqualified: the older-page spinner is the case
// that motivated this rule. It renders above the messages, so mounting it pushes
// the anchored row down and the restore scrolls the viewport right onto the
// spinner; from there it is the worst anchor available, because a prepended page
// lands BELOW it (it never moves, so the restore computes a zero delta) and the
// same commit that adds the page unmounts it (so the restore is skipped for a
// disconnected node). Both paths leave the raw top-of-window ``scrollTop``, which
// is what made paging up jump to the oldest row of the page just loaded — and
// that same near-zero ``scrollTop`` then failed the loader's re-arm gate, so no
// further page could load until the reader scrolled back down and up again.
//
// Rule: anything mounted or unmounted around a load carries
// ``data-scroll-anchor="skip"`` and is never picked.
export type ScrollAnchor = { el: HTMLElement; top: number };

export const pickScrollAnchor = (
  children: readonly HTMLElement[],
  containerTop: number,
): ScrollAnchor | null => {
  for (const child of children) {
    const rect = child.getBoundingClientRect();
    // Entirely above the viewport top — scrolled past, not what the reader sees.
    if (rect.bottom <= containerTop) continue;
    if (child.dataset.scrollAnchor === 'skip') continue;
    return { el: child, top: rect.top - containerTop };
  }
  return null;
};
