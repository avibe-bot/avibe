import { useMemo, useSyncExternalStore } from 'react';

import { isApplePlatform } from './platform';

export type ActionShortcutId = 'voiceInput' | 'showPageAnnotation';

export type ActionShortcut = {
  code: string;
  altKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
};

export type ActionShortcuts = Record<ActionShortcutId, ActionShortcut>;

type ShortcutEvent = Pick<
  KeyboardEvent,
  'altKey' | 'code' | 'ctrlKey' | 'metaKey' | 'shiftKey'
>;

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
    && (shortcut.altKey || shortcut.ctrlKey || shortcut.metaKey)
  );
}

function normalizeActionShortcuts(value: unknown): ActionShortcuts {
  const defaults = defaultActionShortcuts();
  if (!value || typeof value !== 'object' || Array.isArray(value)) return defaults;
  const candidate = value as Partial<Record<ActionShortcutId, unknown>>;
  const voiceInput = isActionShortcut(candidate.voiceInput)
    && !isReservedActionShortcut(candidate.voiceInput)
    ? cloneShortcut(candidate.voiceInput)
    : defaults.voiceInput;
  const showPageAnnotation = isActionShortcut(candidate.showPageAnnotation)
    && !isReservedActionShortcut(candidate.showPageAnnotation)
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

export function shortcutFromKeyboardEvent(event: ShortcutEvent): ActionShortcut | null {
  const shortcut: ActionShortcut = {
    code: event.code,
    altKey: event.altKey,
    ctrlKey: event.ctrlKey,
    metaKey: event.metaKey,
    shiftKey: event.shiftKey,
  };
  return isActionShortcut(shortcut) ? shortcut : null;
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

/** Chords already owned by shell-wide commands must never be assigned twice. */
export function isReservedActionShortcut(shortcut: ActionShortcut): boolean {
  const exactAlt = shortcut.altKey && !shortcut.ctrlKey && !shortcut.metaKey && !shortcut.shiftKey;
  if (exactAlt && (
    shortcut.code === 'KeyW'
    || /^Digit[1-9]$/.test(shortcut.code)
  )) {
    return true;
  }
  // AppShell owns search even when an extra modifier is present.
  if (shortcut.code === 'KeyK' && (shortcut.ctrlKey || shortcut.metaKey)) return true;
  // Window close/minimize accepts Shift but yields when Alt is held.
  if (
    (shortcut.code === 'KeyW' || shortcut.code === 'KeyM')
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
  const key = actionShortcutKeyLabel(shortcut.code);
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
