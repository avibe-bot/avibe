import { useMemo, useSyncExternalStore } from 'react';

import { isApplePlatform } from './platform';

export type ActionShortcutId = 'voiceInput' | 'showPageAnnotation';

export type ActionShortcut = {
  code: string;
  /** Layout-aware key legend captured when the shortcut was assigned. */
  displayKey?: string;
  altKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
};

export type ActionShortcuts = Record<ActionShortcutId, ActionShortcut>;

type ShortcutEvent = Pick<
  KeyboardEvent,
  'altKey' | 'code' | 'ctrlKey' | 'key' | 'metaKey' | 'shiftKey'
>;

type KeyboardLayoutProvider = {
  getLayoutMap: () => Promise<Pick<Map<string, string>, 'get'>>;
};

type ReadableStorage = Pick<Storage, 'getItem'>;
type WritableStorage = Pick<Storage, 'setItem'>;

export const ACTION_SHORTCUTS_STORAGE_KEY = 'avibe.action-shortcuts.v1';
export const ACTION_SHORTCUTS_CHANGED_EVENT = 'avibe:action-shortcuts-changed';

export const DEFAULT_ACTION_SHORTCUTS: Readonly<ActionShortcuts> = {
  voiceInput: {
    code: 'KeyZ',
    altKey: true,
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
  },
  showPageAnnotation: {
    code: 'KeyX',
    altKey: true,
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
  },
};

const MODIFIER_CODES = new Set([
  'AltLeft',
  'AltRight',
  'ControlLeft',
  'ControlRight',
  'MetaLeft',
  'MetaRight',
  'ShiftLeft',
  'ShiftRight',
]);

const BROWSER_RESERVED_PRIMARY_KEYS = new Set([
  'L', // focus location bar
  'N', // new window
  'Q', // quit browser
  'T', // new/reopen tab
]);

const cloneShortcut = (shortcut: ActionShortcut): ActionShortcut => ({ ...shortcut });

export const defaultActionShortcuts = (): ActionShortcuts => ({
  voiceInput: cloneShortcut(DEFAULT_ACTION_SHORTCUTS.voiceInput),
  showPageAnnotation: cloneShortcut(DEFAULT_ACTION_SHORTCUTS.showPageAnnotation),
});

export function actionShortcutsEqual(left: ActionShortcut, right: ActionShortcut): boolean {
  return (
    left.code === right.code
    && left.altKey === right.altKey
    && left.ctrlKey === right.ctrlKey
    && left.metaKey === right.metaKey
    && left.shiftKey === right.shiftKey
  );
}

function isActionShortcut(value: unknown): value is ActionShortcut {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const shortcut = value as Partial<ActionShortcut>;
  return (
    typeof shortcut.code === 'string'
    && shortcut.code.length > 0
    && shortcut.code !== 'Unidentified'
    && !MODIFIER_CODES.has(shortcut.code)
    && typeof shortcut.altKey === 'boolean'
    && typeof shortcut.ctrlKey === 'boolean'
    && typeof shortcut.metaKey === 'boolean'
    && typeof shortcut.shiftKey === 'boolean'
    && (
      shortcut.displayKey === undefined
      || (typeof shortcut.displayKey === 'string' && shortcut.displayKey.length > 0)
    )
    && (shortcut.altKey || shortcut.ctrlKey || shortcut.metaKey)
  );
}

function normalizeActionShortcuts(value: unknown): ActionShortcuts {
  const defaults = defaultActionShortcuts();
  if (!value || typeof value !== 'object' || Array.isArray(value)) return defaults;
  const candidate = value as Partial<Record<ActionShortcutId, unknown>>;
  const voiceInput = isActionShortcut(candidate.voiceInput)
    && !isReservedActionShortcut('voiceInput', candidate.voiceInput)
    ? cloneShortcut(candidate.voiceInput)
    : defaults.voiceInput;
  const showPageAnnotation = isActionShortcut(candidate.showPageAnnotation)
    && !isReservedActionShortcut('showPageAnnotation', candidate.showPageAnnotation)
    ? cloneShortcut(candidate.showPageAnnotation)
    : defaults.showPageAnnotation;
  if (actionShortcutsEqual(voiceInput, showPageAnnotation)) return defaults;
  return { voiceInput, showPageAnnotation };
}

export function readActionShortcuts(storage?: ReadableStorage): ActionShortcuts {
  try {
    const target = storage ?? (typeof window !== 'undefined' ? window.localStorage : undefined);
    const raw = target?.getItem(ACTION_SHORTCUTS_STORAGE_KEY);
    return raw ? normalizeActionShortcuts(JSON.parse(raw)) : defaultActionShortcuts();
  } catch {
    return defaultActionShortcuts();
  }
}

export function writeActionShortcuts(
  shortcuts: ActionShortcuts,
  storage?: WritableStorage,
): void {
  const normalized = normalizeActionShortcuts(shortcuts);
  try {
    const target = storage ?? (typeof window !== 'undefined' ? window.localStorage : undefined);
    target?.setItem(ACTION_SHORTCUTS_STORAGE_KEY, JSON.stringify(normalized));
  } catch {
    return;
  }
  if (typeof window !== 'undefined' && storage === undefined) {
    window.dispatchEvent(new Event(ACTION_SHORTCUTS_CHANGED_EVENT));
  }
}

function normalizedDisplayKey(value: string | undefined): string | undefined {
  if (!value || value === 'Dead' || value === 'Unidentified') return undefined;
  if (value === ' ') return 'Space';
  const glyphs = Array.from(value);
  if (glyphs.length !== 1) return undefined;
  return value.toLocaleUpperCase();
}

export function shortcutFromKeyboardEvent(event: ShortcutEvent): ActionShortcut | null {
  const shortcut: ActionShortcut = {
    code: event.code,
    displayKey: normalizedDisplayKey(event.key),
    altKey: event.altKey,
    ctrlKey: event.ctrlKey,
    metaKey: event.metaKey,
    shiftKey: event.shiftKey,
  };
  return isActionShortcut(shortcut) ? shortcut : null;
}

/** Resolve the physical code to the legend on the active keyboard layout. */
export async function shortcutFromKeyboardEventWithLayout(
  event: ShortcutEvent,
  keyboard: KeyboardLayoutProvider | undefined = (
    typeof navigator === 'undefined'
      ? undefined
      : (navigator as Navigator & { keyboard?: KeyboardLayoutProvider }).keyboard
  ),
): Promise<ActionShortcut | null> {
  // Snapshot before awaiting: callers commonly pass React's native event.
  const shortcut = shortcutFromKeyboardEvent({
    altKey: event.altKey,
    code: event.code,
    ctrlKey: event.ctrlKey,
    key: event.key,
    metaKey: event.metaKey,
    shiftKey: event.shiftKey,
  });
  if (!shortcut || !keyboard) return shortcut;
  try {
    const layout = await keyboard.getLayoutMap();
    const displayKey = normalizedDisplayKey(layout.get(shortcut.code));
    return displayKey ? { ...shortcut, displayKey } : shortcut;
  } catch {
    return shortcut;
  }
}

export function actionShortcutMatches(event: ShortcutEvent, shortcut: ActionShortcut): boolean {
  return (
    event.code === shortcut.code
    && event.altKey === shortcut.altKey
    && event.ctrlKey === shortcut.ctrlKey
    && event.metaKey === shortcut.metaKey
    && event.shiftKey === shortcut.shiftKey
  );
}

function actionShortcutLayoutKey(shortcut: ActionShortcut): string | undefined {
  const displayKey = normalizedDisplayKey(shortcut.displayKey);
  if (displayKey) return displayKey;
  const match = /^Key([A-Z])$/.exec(shortcut.code);
  return match?.[1];
}

function shortcutUsesLayoutKey(shortcut: ActionShortcut, key: string): boolean {
  return actionShortcutLayoutKey(shortcut) === key;
}

/** Chords already owned by the browser, shell, or action surface cannot be assigned. */
export function isReservedActionShortcut(
  id: ActionShortcutId,
  shortcut: ActionShortcut,
): boolean {
  const exactAlt = shortcut.altKey && !shortcut.ctrlKey && !shortcut.metaKey && !shortcut.shiftKey;
  if (exactAlt && (
    shortcut.code === 'KeyW'
    || /^Digit[1-9]$/.test(shortcut.code)
  )) {
    return true;
  }
  // Both Chat composer implementations submit every non-Shift Enter before
  // the window-level voice listener can run.
  if (
    id === 'voiceInput'
    && !shortcut.shiftKey
    && (shortcut.code === 'Enter' || shortcut.code === 'NumpadEnter')
  ) {
    return true;
  }
  // AppShell owns search even when an extra modifier is present.
  if (shortcutUsesLayoutKey(shortcut, 'K') && (shortcut.ctrlKey || shortcut.metaKey)) return true;
  // Browsers and the OS consume these before page content can reliably cancel
  // them, so accepting them would save a shortcut that never fires.
  if (!shortcut.altKey && (shortcut.ctrlKey || shortcut.metaKey) && (
    BROWSER_RESERVED_PRIMARY_KEYS.has(actionShortcutLayoutKey(shortcut) ?? '')
    || /^Digit[0-9]$/.test(shortcut.code)
    || shortcut.code === 'Tab'
  )) {
    return true;
  }
  if (shortcut.code === 'Tab' && (shortcut.altKey || shortcut.metaKey)) return true;
  // Window close/minimize accepts Shift but yields when Alt is held.
  if (
    (shortcutUsesLayoutKey(shortcut, 'W') || shortcutUsesLayoutKey(shortcut, 'M'))
    && !shortcut.altKey
    && (shortcut.ctrlKey || shortcut.metaKey)
  ) {
    return true;
  }
  // Chat archive is exact Ctrl/Command+Shift+D.
  return (
    shortcut.code === 'KeyD'
    && shortcut.shiftKey
    && !shortcut.altKey
    && shortcut.ctrlKey !== shortcut.metaKey
  );
}

const KEY_LABELS: Readonly<Record<string, string>> = {
  ArrowDown: '↓',
  ArrowLeft: '←',
  ArrowRight: '→',
  ArrowUp: '↑',
  Backquote: '`',
  Backslash: '\\',
  Backspace: 'Backspace',
  BracketLeft: '[',
  BracketRight: ']',
  Comma: ',',
  Delete: 'Delete',
  End: 'End',
  Enter: 'Enter',
  Equal: '=',
  Escape: 'Esc',
  Home: 'Home',
  Insert: 'Insert',
  Minus: '-',
  PageDown: 'Page Down',
  PageUp: 'Page Up',
  Period: '.',
  Quote: "'",
  Semicolon: ';',
  Slash: '/',
  Space: 'Space',
  Tab: 'Tab',
};

export function actionShortcutKeyLabel(code: string): string {
  if (/^Key[A-Z]$/.test(code)) return code.slice(3);
  if (/^Digit[0-9]$/.test(code)) return code.slice(5);
  if (/^Numpad[0-9]$/.test(code)) return `Numpad ${code.slice(6)}`;
  return KEY_LABELS[code] ?? code.replace(/([a-z])([A-Z])/g, '$1 $2');
}

export function formatActionShortcut(
  shortcut: ActionShortcut,
  apple = isApplePlatform(),
): string {
  const key = shortcut.displayKey ?? actionShortcutKeyLabel(shortcut.code);
  if (apple) {
    return [
      shortcut.ctrlKey ? '⌃' : '',
      shortcut.altKey ? '⌥' : '',
      shortcut.shiftKey ? '⇧' : '',
      shortcut.metaKey ? '⌘' : '',
      key,
    ].join('');
  }
  return [
    shortcut.ctrlKey ? 'Ctrl' : '',
    shortcut.altKey ? 'Alt' : '',
    shortcut.shiftKey ? 'Shift' : '',
    shortcut.metaKey ? 'Meta' : '',
    key,
  ].filter(Boolean).join('+');
}

const defaultSnapshot = JSON.stringify(defaultActionShortcuts());

function shortcutSnapshot(): string {
  return JSON.stringify(readActionShortcuts());
}

function subscribeToActionShortcuts(listener: () => void): () => void {
  if (typeof window === 'undefined') return () => undefined;
  const onStorage = (event: StorageEvent) => {
    if (event.key === ACTION_SHORTCUTS_STORAGE_KEY) listener();
  };
  window.addEventListener('storage', onStorage);
  window.addEventListener(ACTION_SHORTCUTS_CHANGED_EVENT, listener);
  return () => {
    window.removeEventListener('storage', onStorage);
    window.removeEventListener(ACTION_SHORTCUTS_CHANGED_EVENT, listener);
  };
}

export function useActionShortcuts(): ActionShortcuts {
  const snapshot = useSyncExternalStore(
    subscribeToActionShortcuts,
    shortcutSnapshot,
    () => defaultSnapshot,
  );
  return useMemo(() => normalizeActionShortcuts(JSON.parse(snapshot)), [snapshot]);
}
