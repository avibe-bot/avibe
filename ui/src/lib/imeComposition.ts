/** The signals a key event carries when it belongs to an input method rather
 *  than to the person at the keyboard. Structural on purpose: React's synthetic
 *  event exposes the standard flag on `nativeEvent`, a raw DOM event exposes it
 *  directly, and neither is guaranteed to set it at all. */
export type ComposingKeyEvent = {
  isComposing?: boolean;
  keyCode?: number;
  nativeEvent?: { isComposing?: boolean };
};

/** The legacy IME-in-progress signal, for browsers that leave `isComposing`
 *  unset on the event a key handler receives. */
const LEGACY_COMPOSING_KEY_CODE = 229;

/**
 * Whether this keystroke is the input method's and not the user's.
 *
 * Someone typing Chinese, Japanese, Korean, or Vietnamese types into a
 * candidate window and presses Enter to accept the characters they are
 * composing. The browser delivers that Enter to the field underneath, so a
 * handler that reads Enter as 「commit this」 takes a keystroke that was never
 * aimed at it and commits something the user did not ask for — and Arrow keys
 * move through the candidate list the same way. A handler that acts on keys at
 * all asks this first.
 *
 * Both signals are checked because neither alone is enough: `isComposing` is
 * the standard one, and `keyCode === 229` is what the browsers that omit it
 * from the delivered event set instead.
 *
 * Promoted here on its fifth repeat and the four earlier copies now call it —
 * `TerminalView`, `MentionEditor`, `Composer`, `SearchPalette` — so the next key
 * handler inherits the check instead of rediscovering it. Two of those four read
 * only `isComposing` and were missing the legacy signal, which is the whole
 * argument for one home: the same defect was present four times over and would
 * have been found one review round at a time.
 */
export const isComposingKey = (event: ComposingKeyEvent): boolean =>
  event.nativeEvent?.isComposing === true
  || event.isComposing === true
  || event.keyCode === LEGACY_COMPOSING_KEY_CODE;
