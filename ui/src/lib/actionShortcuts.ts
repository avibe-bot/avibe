import { useMemo, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';

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
  'altKey' | 'code' | 'ctrlKey' | 'key' | 'metaKey' | 'shiftKey'
> & Partial<Pick<KeyboardEvent, 'getModifierState'>>;

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

const cloneShortcut = (shortcut: ActionShortcut): ActionShortcut => ({
  code: shortcut.code,
  altKey: shortcut.altKey,
  ctrlKey: shortcut.ctrlKey,
  metaKey: shortcut.metaKey,
  shiftKey: shortcut.shiftKey,
});

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
): boolean {
  const normalized = normalizeActionShortcuts(shortcuts);
  try {
    const target = storage ?? (typeof window !== 'undefined' ? window.localStorage : undefined);
    if (!target) return false;
    target.setItem(ACTION_SHORTCUTS_STORAGE_KEY, JSON.stringify(normalized));
  } catch {
    return false;
  }
  if (typeof window !== 'undefined' && storage === undefined) {
    window.dispatchEvent(new Event(ACTION_SHORTCUTS_CHANGED_EVENT));
  }
  return true;
}

export function isPlainEscape(
  event: Pick<ShortcutEvent, 'altKey' | 'code' | 'ctrlKey' | 'key' | 'metaKey' | 'shiftKey'>,
): boolean {
  return (
    (event.code === 'Escape' || event.key === 'Escape')
    && !event.altKey
    && !event.ctrlKey
    && !event.metaKey
    && !event.shiftKey
  );
}

export function isAltGraphShortcutEvent(event: Pick<ShortcutEvent, 'getModifierState'>): boolean {
  return event.getModifierState?.('AltGraph') === true;
}

export function shortcutFromKeyboardEvent(event: ShortcutEvent): ActionShortcut | null {
  if (isAltGraphShortcutEvent(event)) return null;
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
    !isAltGraphShortcutEvent(event)
    && event.code === shortcut.code
    && event.altKey === shortcut.altKey
    && event.ctrlKey === shortcut.ctrlKey
    && event.metaKey === shortcut.metaKey
    && event.shiftKey === shortcut.shiftKey
  );
}

/**
 * Chords already owned by the action's own surface cannot be assigned.
 * Browser/OS reservations are intentionally not mirrored here: Settings can
 * persist only keydown events the current browser actually delivers.
 */
export function isReservedActionShortcut(
  id: ActionShortcutId,
  shortcut: ActionShortcut,
): boolean {
  // Both Chat composer implementations submit every non-Shift Enter.
  return (
    id === 'voiceInput'
    && !shortcut.shiftKey
    && (shortcut.code === 'Enter' || shortcut.code === 'NumpadEnter')
  );
}

const SYMBOL_KEY_LABELS: Readonly<Record<string, string>> = {
  ArrowDown: '↓',
  ArrowLeft: '←',
  ArrowRight: '→',
  ArrowUp: '↑',
  Backquote: '`',
  Backslash: '\\',
  BracketLeft: '[',
  BracketRight: ']',
  Comma: ',',
  Equal: '=',
  Minus: '-',
  Period: '.',
  Quote: "'",
  Semicolon: ';',
  Slash: '/',
};

const TRANSLATED_KEY_LABELS: Readonly<Record<string, string>> = {
  Backspace: 'backspace',
  CapsLock: 'capsLock',
  ContextMenu: 'contextMenu',
  Delete: 'delete',
  End: 'end',
  Enter: 'enter',
  Escape: 'escape',
  Home: 'home',
  Insert: 'insert',
  NumLock: 'numLock',
  NumpadEnter: 'numpadEnter',
  PageDown: 'pageDown',
  PageUp: 'pageUp',
  Pause: 'pause',
  PrintScreen: 'printScreen',
  ScrollLock: 'scrollLock',
  Space: 'space',
  Tab: 'tab',
};

export function actionShortcutKeyLabel(code: string, t: TFunction): string {
  if (/^Key[A-Z]$/.test(code)) return code.slice(3);
  if (/^Digit[0-9]$/.test(code)) return code.slice(5);
  if (/^Numpad[0-9]$/.test(code)) {
    return t('settings.shortcuts.keyLabels.numpad', { key: code.slice(6) });
  }
  const numpadSymbol = ({
    NumpadAdd: '+',
    NumpadComma: ',',
    NumpadDecimal: '.',
    NumpadDivide: '/',
    NumpadEqual: '=',
    NumpadMultiply: '×',
    NumpadSubtract: '-',
  } as Readonly<Record<string, string>>)[code];
  if (numpadSymbol) return t('settings.shortcuts.keyLabels.numpad', { key: numpadSymbol });
  const translated = TRANSLATED_KEY_LABELS[code];
  if (translated) return t(`settings.shortcuts.keyLabels.${translated}`);
  return SYMBOL_KEY_LABELS[code] ?? code;
}

export function formatActionShortcut(
  shortcut: ActionShortcut,
  t: TFunction,
  apple = isApplePlatform(),
): string {
  const key = actionShortcutKeyLabel(shortcut.code, t);
  if (apple) {
    return [
      shortcut.ctrlKey ? t('settings.shortcuts.modifierLabels.control') : '',
      shortcut.altKey ? t('settings.shortcuts.modifierLabels.option') : '',
      shortcut.shiftKey ? t('settings.shortcuts.modifierLabels.shift') : '',
      shortcut.metaKey ? t('settings.shortcuts.modifierLabels.command') : '',
      key,
    ].filter(Boolean).join('+');
  }
  return [
    shortcut.ctrlKey ? t('settings.shortcuts.modifierLabels.ctrl') : '',
    shortcut.altKey ? t('settings.shortcuts.modifierLabels.alt') : '',
    shortcut.shiftKey ? t('settings.shortcuts.modifierLabels.shift') : '',
    shortcut.metaKey ? t('settings.shortcuts.modifierLabels.meta') : '',
    key,
  ].filter(Boolean).join('+');
}

export function useActionShortcutLabel(shortcut: ActionShortcut): string {
  const { t } = useTranslation();
  return formatActionShortcut(shortcut, t);
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
