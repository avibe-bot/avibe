// A lightweight cross-tree signal so a previously-failed favicon can be retried
// after the show-page inventory refreshes (§7.1f review). An icon added AFTER its
// `<link rel="icon">` (or a transient 404) leaves the URL unchanged, so the tile
// would otherwise show the letter avatar forever. `useShowPageInventory` fires
// this on every successful load; `ShowPageAvatarContent` subscribes and clears
// its failure. A tiny notifier avoids threading an inventory revision through all
// six avatar surfaces (Dock, mobile drawer, Library, Show Pages, ⌘K, title-bar).

type Listener = () => void;

const listeners = new Set<Listener>();

/** Subscribe to inventory-refresh notifications; returns an unsubscribe fn. */
export function subscribeShowPageIconRefresh(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Notify subscribers that the show-page inventory just refreshed. */
export function notifyShowPageIconRefresh(): void {
  listeners.forEach((listener) => listener());
}
