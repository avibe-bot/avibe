import type { TFunction } from 'i18next';

import {
  actionShortcutMatches,
  actionShortcutKeyLabel,
  shortcutFromKeyboardEvent,
  type ActionShortcut,
} from './actionShortcuts';
import type { DesktopPetSettings } from './desktopShell';

export const desktopPetShortcutFromKeyboardEvent = (
  event: Pick<KeyboardEvent, 'altKey' | 'code' | 'ctrlKey' | 'key' | 'metaKey' | 'shiftKey'>,
): string | null => {
  const shortcut = shortcutFromKeyboardEvent(event);
  if (!shortcut) return null;
  const modifiers = [
    shortcut.ctrlKey ? 'Control' : '',
    shortcut.altKey ? 'Alt' : '',
    shortcut.shiftKey ? 'Shift' : '',
    shortcut.metaKey ? 'Super' : '',
  ].filter(Boolean);
  return [...modifiers, shortcut.code].join('+');
};

export const desktopPetShortcutsEqual = (left: string, right: string): boolean => {
  const normalize = (value: string) => value.split('+').map((part) => part.toLowerCase()).join('+');
  return normalize(left) === normalize(right);
};

export const desktopPetShortcutMatchesAction = (
  shortcut: string,
  action: ActionShortcut,
): boolean => {
  const candidate = shortcut.split('+');
  const code = candidate.at(-1);
  if (!code) return false;
  return actionShortcutMatches(
    {
      code,
      key: code,
      altKey: candidate.some((part) => part.toLowerCase() === 'alt'),
      ctrlKey: candidate.some((part) => part.toLowerCase() === 'control' || part.toLowerCase() === 'ctrl'),
      metaKey: candidate.some((part) => part.toLowerCase() === 'super' || part.toLowerCase() === 'meta'),
      shiftKey: candidate.some((part) => part.toLowerCase() === 'shift'),
    },
    action,
  );
};

export const formatDesktopPetShortcut = (
  shortcut: string,
  t: TFunction,
  apple: boolean,
): string => {
  const parts = shortcut.split('+');
  const key = actionShortcutKeyLabel(parts.at(-1) ?? shortcut, t);
  const labels = parts.slice(0, -1).map((part) => {
    const normalized = part.toLowerCase();
    if (normalized === 'control' || normalized === 'ctrl') {
      return t(apple ? 'settings.shortcuts.modifierLabels.control' : 'settings.shortcuts.modifierLabels.ctrl');
    }
    if (normalized === 'alt' || normalized === 'option') {
      return t(apple ? 'settings.shortcuts.modifierLabels.option' : 'settings.shortcuts.modifierLabels.alt');
    }
    if (normalized === 'shift') return t('settings.shortcuts.modifierLabels.shift');
    if (normalized === 'super' || normalized === 'command' || normalized === 'meta') {
      return t(apple ? 'settings.shortcuts.modifierLabels.command' : 'settings.shortcuts.modifierLabels.meta');
    }
    return part;
  });
  return [...labels, key].join('+');
};

export const desktopPetShortcutConflictsWithActions = (
  shortcut: string,
  settings: Pick<DesktopPetSettings, 'voice_shortcut' | 'toggle_shortcut'>,
  actions: { voiceInput: ActionShortcut; showPageAnnotation: ActionShortcut },
): boolean => (
  desktopPetShortcutsEqual(shortcut, settings.voice_shortcut)
  || desktopPetShortcutsEqual(shortcut, settings.toggle_shortcut)
  || desktopPetShortcutMatchesAction(shortcut, actions.voiceInput)
  || desktopPetShortcutMatchesAction(shortcut, actions.showPageAnnotation)
);
