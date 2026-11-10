// Shared base styling for every form-control surface — Input, Select,
// Textarea, and the Combobox trigger (ui/design: system/fields/all,
// system/elevation/edges). One filled field, so all controls read identically
// when idle:
//   fill   = --field (bg-field), deeper on hover    border = none at rest
//   radius = 8px (rounded-md)                        focus  = mint ring (ring-ring)
// The 1px border stays, transparent (`border-field-edge`), so the box keeps its
// size and returns as a line under `prefers-contrast: more` and forced colours.
// Callers add only size/layout differences (height, padding, chevron) on top
// via cn(fieldBaseClass, ...). Do not re-roll these tokens per component.
export const fieldBaseClass =
  'w-full rounded-md border border-field-edge bg-field text-sm text-foreground transition-colors placeholder:text-muted enabled:hover:not-focus-visible:bg-field-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50';
