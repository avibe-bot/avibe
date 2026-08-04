// Right-hand rail for a session row's ⋯ action trigger. The trigger is absolutely
// positioned, so the row reserves the space only while it is revealed — on hover,
// on keyboard focus, and always on coarse pointers (touch has no hover) or while
// its menu is open. A pinned row no longer forces the rail open: its pin glyph is
// inline with the title, so nothing shifts when the trigger appears.
export const sessionRowActionPaddingClass = (menuOpen: boolean) =>
  menuOpen ? 'pr-10' : 'pr-2.5 hover:pr-10 focus-within:pr-10 pointer-coarse:pr-10';
