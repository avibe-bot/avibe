import { useRef, useState } from 'react';
import { Keyboard, RotateCcw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import {
  actionShortcutsEqual,
  defaultActionShortcuts,
  formatActionShortcut,
  isReservedActionShortcut,
  shortcutFromKeyboardEvent,
  shortcutFromKeyboardEventWithLayout,
  useActionShortcuts,
  writeActionShortcuts,
  type ActionShortcutId,
} from '@/lib/actionShortcuts';
import { Button } from '@/components/ui/button';
import { SettingsPageShell } from './SettingsPageShell';
import { SettingsPanel, SettingsRow } from './SettingsPrimitives';

const SHORTCUT_IDS: readonly ActionShortcutId[] = ['voiceInput', 'showPageAnnotation'];

export const SettingsShortcutsPage: React.FC = () => {
  const { t } = useTranslation();
  const shortcuts = useActionShortcuts();
  const [capturing, setCapturing] = useState<ActionShortcutId | null>(null);
  const [error, setError] = useState<{ id: ActionShortcutId; message: string } | null>(null);
  const captureRequestRef = useRef(0);

  const actionLabel = (id: ActionShortcutId): string => t(`settings.shortcuts.${id}.title`);

  const capture = async (id: ActionShortcutId, event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (capturing !== id) return;
    event.preventDefault();
    event.stopPropagation();
    if (
      event.code === 'Escape'
      && !event.altKey
      && !event.ctrlKey
      && !event.metaKey
      && !event.shiftKey
    ) {
      captureRequestRef.current += 1;
      setCapturing(null);
      setError(null);
      return;
    }

    const next = shortcutFromKeyboardEvent(event.nativeEvent);
    if (!next) {
      setError({ id, message: t('settings.shortcuts.modifierRequired') });
      return;
    }
    if (isReservedActionShortcut(next)) {
      setError({ id, message: t('settings.shortcuts.reserved') });
      return;
    }
    const conflict = SHORTCUT_IDS.find(
      (candidate) => candidate !== id && actionShortcutsEqual(shortcuts[candidate], next),
    );
    if (conflict) {
      setError({
        id,
        message: t('settings.shortcuts.conflict', { action: actionLabel(conflict) }),
      });
      return;
    }

    const request = ++captureRequestRef.current;
    const layoutAware = await shortcutFromKeyboardEventWithLayout(event.nativeEvent);
    if (request !== captureRequestRef.current || !layoutAware) return;
    writeActionShortcuts({ ...shortcuts, [id]: layoutAware });
    setCapturing(null);
    setError(null);
  };

  const shortcutControl = (id: ActionShortcutId) => {
    const active = capturing === id;
    const message = error?.id === id ? error.message : null;
    return (
      <div className="flex min-w-[190px] flex-col items-stretch gap-1.5 md:items-end">
        <button
          type="button"
          data-shortcut-capture
          aria-label={t('settings.shortcuts.change', { action: actionLabel(id) })}
          aria-pressed={active}
          onClick={() => {
            captureRequestRef.current += 1;
            setCapturing(active ? null : id);
            setError(null);
          }}
          onBlur={() => {
            captureRequestRef.current += 1;
            setCapturing((current) => (current === id ? null : current));
          }}
          onKeyDown={(event) => { void capture(id, event); }}
          className={clsx(
            'flex h-9 w-full items-center justify-between gap-3 rounded-lg border bg-surface-2 px-3 text-left transition-colors md:w-[190px]',
            'focus:outline-none focus:ring-2 focus:ring-mint/40',
            active
              ? 'border-mint/60 text-mint-ink shadow-glow-xs-mint'
              : 'border-border-strong text-foreground hover:border-mint/35',
          )}
        >
          <Keyboard className="size-3.5 shrink-0 text-muted" />
          <kbd className="min-w-0 flex-1 truncate text-right font-mono text-[12px] font-semibold">
            {active ? t('settings.shortcuts.pressKeys') : formatActionShortcut(shortcuts[id])}
          </kbd>
        </button>
        <span
          role={message ? 'alert' : undefined}
          className={clsx('min-h-4 text-[10px] leading-4', message ? 'text-destructive-ink' : 'text-muted')}
        >
          {message ?? (active ? t('settings.shortcuts.escapeToCancel') : '')}
        </span>
      </div>
    );
  };

  return (
    <SettingsPageShell
      activeTab="shortcuts"
      title={t('settings.shortcuts.title')}
      subtitle={t('settings.shortcuts.subtitle')}
      actions={
        <Button
          type="button"
          variant="secondary"
          size="xs"
          onClick={() => {
            captureRequestRef.current += 1;
            writeActionShortcuts(defaultActionShortcuts());
            setCapturing(null);
            setError(null);
          }}
        >
          <RotateCcw className="size-3.5" />
          {t('settings.shortcuts.reset')}
        </Button>
      }
    >
      <SettingsPanel
        title={
          <span className="inline-flex items-center gap-2">
            <Keyboard className="size-3.5 text-mint-ink" />
            {t('settings.shortcuts.panelTitle')}
          </span>
        }
        description={t('settings.shortcuts.panelDescription')}
      >
        <SettingsRow
          title={actionLabel('voiceInput')}
          description={t('settings.shortcuts.voiceInput.description')}
          control={shortcutControl('voiceInput')}
        />
        <SettingsRow
          title={actionLabel('showPageAnnotation')}
          description={t('settings.shortcuts.showPageAnnotation.description')}
          control={shortcutControl('showPageAnnotation')}
        />
      </SettingsPanel>
    </SettingsPageShell>
  );
};
