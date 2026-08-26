import { useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';

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
> & Partial<Pick<KeyboardEvent, 'getModifierState'>>;

type KeyboardLayoutProvider = {
  getLayoutMap: () => Promise<Pick<Map<string, string>, 'get'>>;
  addEventListener?: (type: 'layoutchange', listener: EventListener) => void;
  removeEventListener?: (type: 'layoutchange', listener: EventListener) => void;
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
  if (value === ' ') return undefined;
  const glyphs = Array.from(value);
  if (glyphs.length !== 1) return undefined;
  return value.toLocaleUpperCase();
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
  // Read the native event before awaiting; the returned shortcut is the snapshot.
  const shortcut = shortcutFromKeyboardEvent(event);
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
    !isAltGraphShortcutEvent(event)
    && event.code === shortcut.code
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

export function resolveActionShortcutsForLayout(
  shortcuts: ActionShortcuts,
  layoutKeys: Partial<Record<ActionShortcutId, string>>,
): ActionShortcuts {
  const defaults = defaultActionShortcuts();
  const resolve = (id: ActionShortcutId): ActionShortcut => {
    const layoutKey = normalizedDisplayKey(layoutKeys[id]);
    const candidate = layoutKey ? { ...shortcuts[id], displayKey: layoutKey } : shortcuts[id];
    return isReservedActionShortcut(id, candidate) ? defaults[id] : candidate;
  };
  const voiceInput = resolve('voiceInput');
  const showPageAnnotation = resolve('showPageAnnotation');
  return actionShortcutsEqual(voiceInput, showPageAnnotation)
    ? defaults
    : { voiceInput, showPageAnnotation };
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
  layoutKey?: string,
): string {
  const key = shortcut.code.startsWith('Numpad')
    ? actionShortcutKeyLabel(shortcut.code, t)
    : (
      normalizedDisplayKey(layoutKey)
      ?? normalizedDisplayKey(shortcut.displayKey)
      ?? actionShortcutKeyLabel(shortcut.code, t)
    );
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
    shortcut.ctrlKey ? t('settings.shortcuts.modifierLabels.ctrl') : '',
    shortcut.altKey ? t('settings.shortcuts.modifierLabels.alt') : '',
    shortcut.shiftKey ? t('settings.shortcuts.modifierLabels.shift') : '',
    shortcut.metaKey ? t('settings.shortcuts.modifierLabels.meta') : '',
    key,
  ].filter(Boolean).join('+');
}

function activeKeyboardLayoutProvider(): KeyboardLayoutProvider | undefined {
  if (typeof navigator === 'undefined') return undefined;
  return (navigator as Navigator & { keyboard?: KeyboardLayoutProvider }).keyboard;
}

function useActiveLayoutKey(code: string): string | undefined {
  const [resolved, setResolved] = useState<{ code: string; key?: string }>({ code: '' });

  useEffect(() => {
    const keyboard = activeKeyboardLayoutProvider();
    let active = true;
    let request = 0;

    const refresh = () => {
      const currentRequest = ++request;
      if (!keyboard) {
        setResolved({ code });
        return;
      }
      void keyboard.getLayoutMap().then((layout) => {
        if (!active || currentRequest !== request) return;
        setResolved({ code, key: normalizedDisplayKey(layout.get(code)) });
      }).catch(() => {
        if (!active || currentRequest !== request) return;
        setResolved({ code });
      });
    };
    const onLayoutChange: EventListener = () => refresh();

    refresh();
    keyboard?.addEventListener?.('layoutchange', onLayoutChange);
    return () => {
      active = false;
      keyboard?.removeEventListener?.('layoutchange', onLayoutChange);
    };
  }, [code]);

  return resolved.code === code ? resolved.key : undefined;
}

export function useActionShortcutLabel(shortcut: ActionShortcut): string {
  const { t } = useTranslation();
  const layoutKey = useActiveLayoutKey(shortcut.code);
  return formatActionShortcut(shortcut, t, isApplePlatform(), layoutKey);
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
  const stored = useMemo(() => normalizeActionShortcuts(JSON.parse(snapshot)), [snapshot]);
  const voiceInputLayoutKey = useActiveLayoutKey(stored.voiceInput.code);
  const annotationLayoutKey = useActiveLayoutKey(stored.showPageAnnotation.code);
  return useMemo(
    () => resolveActionShortcutsForLayout(stored, {
      voiceInput: voiceInputLayoutKey,
      showPageAnnotation: annotationLayoutKey,
    }),
    [annotationLayoutKey, stored, voiceInputLayoutKey],
  );
}
