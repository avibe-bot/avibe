// Compact right-hand rail shared by the direct pin control and action menu.
// Both controls use the same footprint/radius; the menu sits close to the row
// edge and the pin follows without a gap between their hit areas.
export const SESSION_ROW_ACTION_BUTTON_CLASS = 'size-5 rounded-md';
export const SESSION_ROW_PIN_POSITION_CLASS = 'right-5';
export const SESSION_ROW_MENU_POSITION_CLASS = 'right-0';

// Pinned rows reserve the rail at rest; unpinned rows expand it only while
// either control is revealed through hover/focus or while the menu is open.
export const sessionRowActionPaddingClass = (menuOpen: boolean, pinned: boolean) =>
  menuOpen || pinned ? 'pr-11' : 'pr-2.5 hover:pr-11 focus-within:pr-11 pointer-coarse:pr-11';
