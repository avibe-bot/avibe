// The two message-bubble shapes the chat column draws, in one place.
//
// design.pen TxFKk rule 03 says the annotation card's bubble IS the existing
// chat bubble — same width rule, same radius, same timestamp — because an
// annotation is a message, not a widget, and a second bubble shape in the same
// column reads as a different product. The frame backs that literally: its
// annotation bubbles are byte-identical to the ordinary user / Claude bubbles
// drawn beside them.
//
// Naming these makes that rule a fact instead of a comment: the card cannot
// drift from the rows around it, because there is nothing to drift from.
//
// Shape is shared; only the corner notch (which points back at the head above
// it) and the tint depend on which side the row takes.
const BUBBLE_BASE =
  'w-fit min-w-0 max-w-full rounded-2xl border px-3.5 py-2.5 leading-relaxed [&_pre]:max-w-full [&_pre]:overflow-x-auto [&_table]:w-full';
const BUBBLE_RIGHT = `${BUBBLE_BASE} rounded-tr-md`;
const BUBBLE_LEFT = `${BUBBLE_BASE} rounded-tl-md`;

/** Right-aligned neutral bubble — the user's own words. */
export const USER_BUBBLE = `${BUBBLE_RIGHT} border-border-strong bg-foreground/[0.06]`;

/** Left-aligned mint bubble — the agent's own words. */
export const AGENT_BUBBLE = `${BUBBLE_LEFT} border-mint/25 bg-mint/[0.09]`;

/** Left-aligned neutral bubble — a system row, quieter than the agent's. */
export const SYSTEM_BUBBLE = `${BUBBLE_LEFT} border-border bg-foreground/[0.03]`;
