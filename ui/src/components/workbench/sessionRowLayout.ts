// Compact right-hand rail shared by the direct pin control and action menu.
// Both controls use the same footprint/radius; the menu sits close to the row
// edge and the pin follows without a gap between their hit areas.
export const SESSION_ROW_ACTION_BUTTON_CLASS = 'size-5 rounded-md';
export const SESSION_ROW_PIN_POSITION_CLASS = 'right-5';
export const SESSION_ROW_MENU_POSITION_CLASS = 'right-0';

// The desktop session row's left inset, and the one owner of it. Every row
// carries the selected accent's 2px border and only its colour changes with
// selection, so the status dot and the name keep one X coordinate. Paying for
// that border with a second `pl-*` on the selected row cannot work: utilities
// of equal specificity are resolved by their order in the generated stylesheet
// rather than by the order of the class string, and Tailwind emitted the 26px
// padding after the 24px one — so the compensation never applied and selecting
// a row pushed its contents 2px to the right.
export const SESSION_ROW_INDENT_CLASS = 'border-l-2 pl-[24px]';

// A running session's dot pulses, so the user can see which session is working
// while reading another chat. `animate-pulse` is a ~2s opacity cycle that keeps
// the dot visible throughout and moves no geometry; under reduced motion the
// dot falls back to its static status colour. Shared by the desktop sidebar and
// the mobile session list so the two surfaces cannot drift apart.
export const SESSION_STATUS_DOT_MOTION_CLASS = 'animate-pulse motion-reduce:animate-none';
