/**
 * The outside-click rule for form dialogs — a dialog the user types or pastes
 * into does not close on a click outside it — for a dialog built on the Radix
 * primitive directly: pass it as `onPointerDownOutside`. `DialogContent` callers
 * use its `keepOnOutsideClick` prop, which applies this.
 */
export const keepOpenOnOutsideClick = (event: Event) => event.preventDefault();
