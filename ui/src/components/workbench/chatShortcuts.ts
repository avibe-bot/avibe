import { isApplePlatform } from '../../lib/platform';

// Chat-page keyboard chords, kept as pure matchers (same shape as
// apps/dockShortcuts.ts and apps/windowChords.ts) so the exactness of each chord
// is unit-testable without a DOM.

type ChordEvent = Pick<KeyboardEvent, 'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey' | 'code'>;

const SHORTCUT_BLOCKING_OVERLAY_SELECTOR =
  '[data-shortcut-capture], [data-state="open"], [role="menu"], [aria-expanded="true"][aria-haspopup], [role="dialog"]:not([data-window-id]), dialog[open]';

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

/**
 * True when the keystroke belongs to a surface stacked ON TOP of the chat rather
 * than to the chat itself.
 *
 * The chat stays mounted under an app window (Terminal, Editor, Files…) and under
 * every dialog, so "ChatPage is mounted" is not "chat owns the keyboard": without
 * this, ⌘⇧D typed into a Terminal window or a Settings dialog would open the
 * archive prompt for whatever session happens to be behind it (Codex).
 *
 * Duck-typed on `closest` (like apps/windowChords.ts) so a non-Element target —
 * `window`, `document` — is simply not foreign, and elements from a same-origin
 * iframe realm still work.
 */
export function inForegroundSurface(el: Element | null): boolean {
  return !!el?.closest?.('[data-window-id], [data-window-owner-id], [role="dialog"], [role="alertdialog"]');
}

/**
 * True when a temporary overlay owns keyboard input. The document-level menu
 * check covers custom menus that keep focus on their trigger while open.
 */
export function inShortcutBlockingOverlay(
  el: Element | null,
  root?: Pick<Document, 'querySelector'>,
): boolean {
  return (
    !!el?.closest?.(SHORTCUT_BLOCKING_OVERLAY_SELECTOR)
    || !!root?.querySelector('[role="menu"]')
  );
}

/**
 * The whole window-level decision for the archive chord: it is our chord, AND the
 * keystroke belongs to the chat rather than to a surface stacked on top of it.
 * ChatPage binds this on `window` only while the session is actually archivable, so
 * a read-only or still-loading chat never consumes the browser's ⌘⇧D either.
 */
export function isArchiveSessionKeydown(event: ChordEvent, target: Element | null): boolean {
  return isArchiveSessionChord(event) && !inForegroundSurface(target);
}

/** Display label for the archive chord, shown as the menu row's hint badge. */
export function archiveSessionShortcutLabel(): string {
  return isApplePlatform() ? '⇧⌘D' : 'Ctrl+Shift+D';
}
