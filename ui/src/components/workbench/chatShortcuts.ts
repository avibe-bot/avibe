import { isApplePlatform } from '../../lib/platform';

// Chat-page keyboard chords, kept as pure matchers (same shape as
// apps/dockShortcuts.ts and apps/windowChords.ts) so the exactness of each chord
// is unit-testable without a DOM.

type ChordEvent = Pick<KeyboardEvent, 'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey' | 'code'>;

/**
 * ⌘⇧D / Ctrl+Shift+D — archive the session the user is reading.
 *
 * Matched on ``code`` so a non-QWERTY layout still resolves the physical D, and
 * required EXACT: Alt must be clear, and the other modifier must not be held too
 * (Ctrl+⌘⇧D is somebody else's chord, not ours). The caller opens the archive
 * confirm dialog — a destructive action never fires straight off a keystroke.
 */
export function isArchiveSessionChord(event: ChordEvent): boolean {
  if (event.altKey || !event.shiftKey || event.code !== 'KeyD') return false;
  // Exactly one of meta / ctrl, so ⌘ works on macOS and Ctrl everywhere else.
  return event.metaKey !== event.ctrlKey;
}

/** Display label for the archive chord, shown as the menu row's hint badge. */
export function archiveSessionShortcutLabel(): string {
  return isApplePlatform() ? '⇧⌘D' : 'Ctrl+Shift+D';
}
