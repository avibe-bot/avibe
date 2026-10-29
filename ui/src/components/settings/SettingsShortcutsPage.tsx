import { useState } from 'react';
import { Keyboard, RotateCcw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import {
  actionShortcutsEqual,
  defaultActionShortcuts,
  isAltGraphShortcutEvent,
  isReservedActionShortcut,
  isPlainEscape,
  readActionShortcuts,
  shortcutFromKeyboardEvent,
  useActionShortcutLabel,
  useActionShortcuts,
  writeActionShortcuts,
  type ActionShortcutId,
} from '@/lib/actionShortcuts';
import { isApplePlatform } from '@/lib/platform';
import {
  desktopPetShortcutFromKeyboardEvent,
  desktopPetShortcutMatchesAction,
  desktopPetShortcutsEqual,
  formatDesktopPetShortcut,
} from '@/lib/desktopPetShortcuts';
import { useDesktopPetSettings } from '@/lib/useDesktopPetSettings';
import { Button } from '@/components/ui/button';
import { Select } from '@/components/ui/select';
import { SettingsPageShell } from './SettingsPageShell';
import { SettingsPanel, SettingsRow } from './SettingsPrimitives';

const SHORTCUT_IDS: readonly ActionShortcutId[] = ['voiceInput', 'showPageAnnotation'];
type DesktopShortcutId = 'desktopVoice' | 'desktopToggle';
type CaptureId = ActionShortcutId | DesktopShortcutId;
const DEFAULT_DESKTOP_VOICE_SHORTCUT = 'Control+Alt+Space';
const DEFAULT_DESKTOP_TOGGLE_SHORTCUT = 'Control+Alt+KeyM';

export const SettingsShortcutsPage: React.FC = () => {
  const { t } = useTranslation();
  const shortcuts = useActionShortcuts();
  const pet = useDesktopPetSettings();
  const [capturing, setCapturing] = useState<CaptureId | null>(null);
  const [error, setError] = useState<{ id: CaptureId; message: string } | null>(null);
  const [persistError, setPersistError] = useState<string | null>(null);
  const shortcutLabels: Record<ActionShortcutId, string> = {
    voiceInput: useActionShortcutLabel(shortcuts.voiceInput),
    showPageAnnotation: useActionShortcutLabel(shortcuts.showPageAnnotation),
  };

  const actionLabel = (id: ActionShortcutId): string => t(`settings.shortcuts.${id}.title`);
  const desktopActionLabel = (id: DesktopShortcutId): string => t(
    id === 'desktopVoice' ? 'settings.shortcuts.desktopMiniVoice.title' : 'settings.shortcuts.desktopMiniToggle.title',
  );

  const capture = (id: CaptureId, event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (capturing !== id) return;
    event.preventDefault();
    event.stopPropagation();
    if (isPlainEscape(event)) {
      setCapturing(null);
      setError(null);
      return;
    }
    if (isAltGraphShortcutEvent(event.nativeEvent)) {
      setError({ id, message: t('settings.shortcuts.reserved') });
      return;
    }

    if (id === 'desktopVoice' || id === 'desktopToggle') {
      if (!pet.state || !pet.actions) return;
      const next = desktopPetShortcutFromKeyboardEvent(event.nativeEvent);
      if (!next) {
        setError({ id, message: t('settings.shortcuts.modifierRequired') });
        return;
      }
      const other = id === 'desktopVoice' ? pet.state.toggle_shortcut : pet.state.voice_shortcut;
      if (desktopPetShortcutsEqual(next, other)) {
        setError({
          id,
          message: t('settings.shortcuts.conflict', { action: desktopActionLabel(id === 'desktopVoice' ? 'desktopToggle' : 'desktopVoice') }),
        });
        return;
      }
      const actionShortcuts = readActionShortcuts();
      if (
        desktopPetShortcutMatchesAction(next, actionShortcuts.voiceInput)
        || desktopPetShortcutMatchesAction(next, actionShortcuts.showPageAnnotation)
      ) {
        const conflict = desktopPetShortcutMatchesAction(next, actionShortcuts.voiceInput)
          ? 'voiceInput'
          : 'showPageAnnotation';
        setError({ id, message: t('settings.shortcuts.conflict', { action: actionLabel(conflict) }) });
        return;
      }
      const target = id === 'desktopVoice' ? 'voice' : 'toggle';
      setCapturing(null);
      setError(null);
      void pet.apply(() => pet.actions!.setShortcut(target, next));
      return;
    }

    const next = shortcutFromKeyboardEvent(event.nativeEvent);
    if (!next) {
      setError({ id, message: t('settings.shortcuts.modifierRequired') });
      return;
    }
    if (isReservedActionShortcut(id, next)) {
      setError({ id, message: t('settings.shortcuts.reserved') });
      return;
    }
    const storedShortcuts = readActionShortcuts();
    const conflict = SHORTCUT_IDS.find(
      (candidate) => candidate !== id && actionShortcutsEqual(storedShortcuts[candidate], next),
    );
    if (conflict) {
      setError({
        id,
        message: t('settings.shortcuts.conflict', { action: actionLabel(conflict) }),
      });
      return;
    }
    if (
      pet.state
      && (
        desktopPetShortcutMatchesAction(pet.state.voice_shortcut, next)
        || desktopPetShortcutMatchesAction(pet.state.toggle_shortcut, next)
      )
    ) {
      const conflict = desktopPetShortcutMatchesAction(pet.state.voice_shortcut, next)
        ? 'desktopVoice'
        : 'desktopToggle';
      setError({ id, message: t('settings.shortcuts.conflict', { action: desktopActionLabel(conflict) }) });
      return;
    }

    if (!writeActionShortcuts({ ...storedShortcuts, [id]: next })) {
      setPersistError(t('settings.shortcuts.saveFailed'));
      return;
    }
    setPersistError(null);
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
          data-shortcut-capture={active ? 'active' : undefined}
          aria-label={t('settings.shortcuts.change', { action: actionLabel(id) })}
          aria-pressed={active}
          onClick={() => {
            setCapturing(active ? null : id);
            setError(null);
            setPersistError(null);
          }}
          onBlur={() => {
            setCapturing((current) => (current === id ? null : current));
          }}
          onKeyDown={(event) => capture(id, event)}
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
            {active ? t('settings.shortcuts.pressKeys') : shortcutLabels[id]}
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

  const desktopShortcutControl = (id: DesktopShortcutId) => {
    const active = capturing === id;
    const message = error?.id === id ? error.message : null;
    const shortcut = id === 'desktopVoice' ? pet.state?.voice_shortcut : pet.state?.toggle_shortcut;
    const nativeError = pet.state?.error?.target === (id === 'desktopVoice' ? 'voice' : 'toggle')
      ? pet.state.error
      : null;
    const label = shortcut
      ? formatDesktopPetShortcut(shortcut, t, isApplePlatform())
      : t('settings.shortcuts.loading');
    return (
      <div className="flex min-w-[190px] flex-col items-stretch gap-1.5 md:items-end">
        <button
          type="button"
          data-shortcut-capture={active ? 'active' : undefined}
          aria-label={t('settings.shortcuts.change', { action: desktopActionLabel(id) })}
          aria-pressed={active}
          disabled={!pet.state || pet.loading}
          onClick={() => {
            setCapturing(active ? null : id);
            setError(null);
            setPersistError(null);
          }}
          onBlur={() => setCapturing((current) => (current === id ? null : current))}
          onKeyDown={(event) => capture(id, event)}
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
            {active ? t('settings.shortcuts.pressKeys') : label}
          </kbd>
        </button>
        <span
          role={message || nativeError ? 'alert' : undefined}
          className={clsx('min-h-4 text-[10px] leading-4', message || nativeError ? 'text-destructive-ink' : 'text-muted')}
        >
          {message
            ?? (nativeError
              ? t('settings.shortcuts.registrationFailed', { shortcut: formatDesktopPetShortcut(nativeError.shortcut, t, isApplePlatform()) })
              : active ? t('settings.shortcuts.escapeToCancel') : '')}
        </span>
      </div>
    );
  };

  const reset = () => {
    const previousActionShortcuts = readActionShortcuts();
    setCapturing(null);
    setError(null);
    if (!pet.actions) {
      if (!writeActionShortcuts(defaultActionShortcuts())) {
        setPersistError(t('settings.shortcuts.saveFailed'));
        return;
      }
      setPersistError(null);
      return;
    }
    void pet.apply(async () => {
      let state = await pet.actions!.setShortcut('voice', DEFAULT_DESKTOP_VOICE_SHORTCUT);
      if (state.error) {
        writeActionShortcuts(previousActionShortcuts);
        return state;
      }
      state = await pet.actions!.setShortcut('toggle', DEFAULT_DESKTOP_TOGGLE_SHORTCUT);
      if (state.error) {
        writeActionShortcuts(previousActionShortcuts);
        return state;
      }
      state = await pet.actions!.setVoiceMode('hold');
      if (state.error) {
        writeActionShortcuts(previousActionShortcuts);
        return state;
      }
      if (!writeActionShortcuts(defaultActionShortcuts())) {
        setPersistError(t('settings.shortcuts.saveFailed'));
        return state;
      }
      setPersistError(null);
      // Keep the browser copy aligned with the native result. The native
      // commands have already accepted all three defaults before this write.
      return state;
    });
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
          onClick={reset}
        >
          <RotateCcw className="size-3.5" />
          {t('settings.shortcuts.reset')}
        </Button>
      }
    >
      {persistError && (
        <p role="alert" className="text-xs text-destructive-ink">
          {persistError}
        </p>
      )}
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
      {pet.supported && (
        <SettingsPanel
          title={
            <span className="inline-flex items-center gap-2">
              <Keyboard className="size-3.5 text-mint-ink" />
              {t('settings.shortcuts.desktopMiniTitle')}
            </span>
          }
          description={t('settings.shortcuts.desktopMiniDescription')}
        >
          <SettingsRow
            title={desktopActionLabel('desktopVoice')}
            description={t('settings.shortcuts.desktopMiniVoice.description')}
            control={desktopShortcutControl('desktopVoice')}
          />
          <SettingsRow
            title={desktopActionLabel('desktopToggle')}
            description={t('settings.shortcuts.desktopMiniToggle.description')}
            control={desktopShortcutControl('desktopToggle')}
          />
          <SettingsRow
            title={t('settings.shortcuts.desktopMiniMode.title')}
            description={t('settings.shortcuts.desktopMiniMode.description')}
            control={(
              <Select
                aria-label={t('settings.shortcuts.desktopMiniMode.title')}
                value={pet.state?.voice_mode ?? 'hold'}
                disabled={!pet.state || pet.loading}
                onChange={(event) => {
                  if (!pet.actions) return;
                  void pet.apply(() => pet.actions!.setVoiceMode(event.target.value as 'hold' | 'toggle'));
                }}
                wrapperClassName="w-[190px] max-w-full"
                className="h-9 rounded-lg border-border-strong bg-surface-2 px-3 text-[12px]"
              >
                <option value="hold">{t('settings.shortcuts.desktopMiniMode.hold')}</option>
                <option value="toggle">{t('settings.shortcuts.desktopMiniMode.toggle')}</option>
              </Select>
            )}
          />
        </SettingsPanel>
      )}
    </SettingsPageShell>
  );
};
