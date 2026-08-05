// Right-hand rail for the direct pin control and the session action menu. Pinned
// rows reserve it at rest; unpinned rows expand it only while either control is
// revealed through hover/focus or while the menu is open.
export const sessionRowActionPaddingClass = (menuOpen: boolean, pinned: boolean) =>
  menuOpen || pinned ? 'pr-16' : 'pr-2.5 hover:pr-16 focus-within:pr-16 pointer-coarse:pr-16';
