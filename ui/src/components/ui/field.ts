// Shared base styling for every form-control surface — Input, Select,
// Textarea, and the Combobox trigger (ui/design: system/fields/all). One
// "inset field" treatment, so all controls read identically when idle:
//   fill   = --background (bg-background)    border = --input (border-input)
//   radius = 8px (rounded-md)                focus  = mint ring (ring-ring)
// Callers add only size/layout differences (height, padding, chevron) on top
// via cn(fieldBaseClass, ...). Do not re-roll these tokens per component.
export const fieldBaseClass =
  'w-full rounded-md border border-input bg-background text-sm text-foreground shadow-sm transition-colors placeholder:text-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring disabled:cursor-not-allowed disabled:opacity-50';
