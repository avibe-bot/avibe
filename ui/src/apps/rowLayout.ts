// Shared trailing action-zone class for App Library rows — the Apps view
// (LibraryApp) and the Show Pages view (ShowPagesPage). A FIXED width so the
// kind/status badge that sits immediately to its left keeps a stable right-edge
// column across every row AND both views (§7.1h item 1): the buttons right-justify
// within the zone, so a longer/shorter toggle label — or a conditional 移出 —
// never shifts the badge. One constant, so the two views can't drift apart.
export const SHARED_ACTION_ZONE = 'flex shrink-0 items-center justify-end gap-2 sm:w-36';
