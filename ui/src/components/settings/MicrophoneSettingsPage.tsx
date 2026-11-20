import { AlertCircle, Check, ChevronDown, LoaderCircle, Mic, ShieldCheck, Square } from 'lucide-react';
import clsx from 'clsx';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { isApplePlatform, isIosDevice } from '@/lib/platform';
import { isDesktopShell } from '@/lib/desktopShell';
import {
  listVoiceInputDevices,
  readVoiceInputSelection,
  SYSTEM_DEFAULT_VOICE_INPUT,
  writeVoiceInputSelection,
  type VoiceInputDevice,
  type VoiceInputSelection,
} from '@/lib/voiceInput';
import { startVoiceInputTest, type VoiceInputTest } from '@/lib/voiceInputTest';
import type { VoiceLevelSource } from '@/lib/voiceLevel';
import { Button } from '@/components/ui/button';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { VoiceMeter } from '@/components/voice/VoiceMeter';
import { SettingsPageShell } from './SettingsPageShell';
import { SettingsPanel } from './SettingsPrimitives';
import { useToast } from '@/context/ToastContext';

export type MicrophoneSettingsState =
  | 'default'
  | 'selected'
  | 'picker'
  | 'testing'
  | 'quiet'
  | 'prompt'
  | 'requesting'
  | 'denied'
  | 'missing'
  | 'disconnected';

export type MicrophoneSettingsViewProps = {
  platform: 'desktop' | 'ios';
  state: MicrophoneSettingsState;
  selection: VoiceInputSelection;
  devices: VoiceInputDevice[];
  missing: boolean;
  level?: VoiceLevelSource | null;
  onSelect?: (selection: VoiceInputSelection) => void;
  onStart?: () => void;
  onStop?: () => void;
  onPermissionAction?: () => void;
};

const stateIsTesting = (state: MicrophoneSettingsState): boolean => state === 'testing' || state === 'quiet';
const stateIsPermission = (state: MicrophoneSettingsState): boolean => (
  state === 'prompt' || state === 'requesting' || state === 'denied'
);

const desktopPermissionRecoveryKey = () => (
  isDesktopShell()
    ? isApplePlatform() ? 'voice.deniedDesktop' : 'voice.deniedDesktopOther'
    : 'voice.composer.deniedBrowser'
);

export const MicrophoneSettingsView: React.FC<MicrophoneSettingsViewProps> = ({
  platform,
  state,
  selection,
  devices,
  missing,
  level,
  onSelect,
  onStart,
  onStop,
  onPermissionAction,
}) => {
  const { t } = useTranslation();
  const ios = platform === 'ios';
  const testing = stateIsTesting(state);
  const permission = stateIsPermission(state);
  const [open, setOpen] = useState(state === 'picker');
  const selectionLabel = selection === SYSTEM_DEFAULT_VOICE_INPUT
    ? t('settings.microphone.systemDefault')
    : devices.find((device) => device.deviceId === selection)?.label || t('settings.microphone.deviceFallback');
  const options = useMemo(() => [
    {
      id: SYSTEM_DEFAULT_VOICE_INPUT as VoiceInputSelection,
      title: t('settings.microphone.systemDefault'),
      detail: t('settings.microphone.followSystem'),
    },
    ...devices.map((device) => ({
      id: device.deviceId,
      title: device.label || t('settings.microphone.deviceFallback'),
      detail: t('settings.microphone.inputDevice'),
    })),
  ], [devices, t]);
  const status = testing
    ? state === 'quiet'
      ? t('settings.microphone.testingQuiet')
      : t('settings.microphone.testingHeard')
    : state === 'disconnected'
      ? t('settings.microphone.testStopped')
      : t('settings.microphone.notTested');

  return (
    <SettingsPageShell
      title={t('settings.microphone.title')}
      subtitle={ios ? t('settings.microphone.iosSubtitle') : t('settings.microphone.subtitle')}
      activeTab="microphone"
    >
      <SettingsPanel variant="preference">
        <div className="flex flex-col gap-5">
          <div className="flex flex-col gap-2">
            <span className="text-xs font-medium text-foreground">{t('settings.microphone.inputTitle')}</span>
            {ios ? (
              <div className="flex items-center gap-3 py-1">
                <Mic className="size-5 text-muted" />
                <div className="flex flex-col gap-1">
                  <span className="text-sm font-medium">{t('settings.microphone.systemInput')}</span>
                  <span className="text-xs text-muted">{t('settings.microphone.iosInputDescription')}</span>
                </div>
              </div>
            ) : (
              <Popover open={open} onOpenChange={(open) => {
                setOpen(open);
              }}>
                <PopoverTrigger asChild>
                  <Button
                    variant="outline"
                    className="h-auto min-h-12 w-full justify-between gap-3 px-3 py-3 text-left"
                    disabled={permission || testing}
                    aria-label={t('settings.microphone.chooseInput')}
                  >
                    <span className="flex min-w-0 items-center gap-3">
                      <Mic className="size-4 text-muted" />
                      <span className="truncate">{permission ? t('settings.microphone.systemDefault') : selectionLabel}</span>
                    </span>
                    <span className="flex shrink-0 items-center gap-2">
                      <span className="text-xs font-normal text-muted">
                        {missing
                          ? t('settings.microphone.unavailable')
                          : selection === SYSTEM_DEFAULT_VOICE_INPUT
                            ? t('settings.microphone.followSystem')
                            : ''}
                      </span>
                      <ChevronDown className="size-4 text-muted" />
                    </span>
                  </Button>
                </PopoverTrigger>
                <PopoverContent align="start" className="w-80 max-w-full p-2" onOpenAutoFocus={(event) => event.preventDefault()}>
                  <p className="px-3 py-2 text-xs text-muted">
                    {t('settings.microphone.chooseThenTest')}
                  </p>
                  <div className="flex flex-col gap-1" role="group" aria-label={t('settings.microphone.deviceList')}>
                    {options.map((option) => (
                      <Button
                        key={option.id}
                        variant="ghost"
                        aria-pressed={option.id === selection}
                        onClick={() => {
                          onSelect?.(option.id);
                          setOpen(false);
                        }}
                        className={clsx(
                          'h-auto min-h-14 w-full justify-between gap-2 whitespace-normal border px-3 py-3 text-left',
                          option.id === selection ? 'border-mint/50 bg-mint-soft' : 'border-transparent',
                        )}
                      >
                        <span className="flex min-w-0 flex-col gap-1">
                          <span className="text-sm">{option.title}</span>
                          <span className="text-xs font-normal text-muted">{option.detail}</span>
                        </span>
                        {option.id === selection && <Check className="size-4 shrink-0 text-mint-ink" />}
                      </Button>
                    ))}
                  </div>
                </PopoverContent>
              </Popover>
            )}
            {!ios && !permission && !missing && (
              <p className="text-xs text-muted">
                {selection === SYSTEM_DEFAULT_VOICE_INPUT
                  ? t('settings.microphone.systemDefaultDescription')
                  : t('settings.microphone.selectedDescription')}
              </p>
            )}
          </div>

          {missing && (
            <div className="flex gap-2 text-sm" role="status">
              <AlertCircle className="mt-0.5 size-4 shrink-0 text-gold-ink" />
              <div className="flex flex-col gap-1">
                <p className="font-medium">{t('settings.microphone.missingTitle')}</p>
                <p className="text-xs leading-relaxed text-muted">{t('settings.microphone.missingDescription')}</p>
              </div>
            </div>
          )}

          {permission ? (
            <div className="flex flex-col items-start gap-3 border-t border-border pt-5">
              <div className="flex flex-col gap-1">
                <p className="text-sm font-medium">
                  {state === 'denied'
                    ? t('settings.microphone.permissionDeniedTitle')
                    : state === 'requesting'
                      ? t('settings.microphone.permissionRequestingTitle')
                      : t('settings.microphone.permissionPromptTitle')}
                </p>
                <p className="text-xs leading-relaxed text-muted">
                  {state === 'denied'
                    ? t(ios ? 'settings.microphone.permissionDeniedIos' : desktopPermissionRecoveryKey())
                    : state === 'requesting'
                      ? t('settings.microphone.permissionRequestingDescription')
                      : t('settings.microphone.permissionPromptDescription')}
                </p>
              </div>
              {state === 'requesting'
                ? (
                  <div className="flex items-center gap-2 text-xs text-muted">
                    <LoaderCircle className="size-4 animate-spin" />
                    {t('settings.microphone.permissionWaiting')}
                  </div>
                )
                : (
                  <Button className="min-h-11" onClick={onPermissionAction}>
                    {state === 'denied'
                      ? t('settings.microphone.recheckPermission')
                      : t('settings.microphone.allowPermission')}
                  </Button>
                )}
            </div>
          ) : (
            <div className="flex flex-col gap-4 border-t border-border pt-5">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex flex-col gap-2" role="status" aria-live="polite">
                  <span className={clsx('text-sm font-medium', state === 'testing' && 'text-mint-ink')}>
                    {status}
                  </span>
                  {testing
                    ? <div data-testid="microphone-level"><VoiceLevelMeter level={level} /></div>
                    : <span className="text-xs text-muted">{t('settings.microphone.microphoneOff')}</span>}
                </div>
                <Button
                  variant={testing ? 'secondary' : 'default'}
                  className="min-h-11 self-start sm:self-center"
                  onClick={testing ? onStop : onStart}
                >
                  {testing ? <Square className="size-3.5" /> : <Mic className="size-3.5" />}
                  {testing ? t('settings.microphone.stopTest') : t('settings.microphone.startTest')}
                </Button>
              </div>
              {testing && (
                <p className="text-xs leading-relaxed text-muted">
                  {state === 'quiet'
                    ? t('settings.microphone.quietDescription')
                    : t('settings.microphone.testingDescription')}
                </p>
              )}
            </div>
          )}

          <div className="flex items-start gap-2 text-xs leading-relaxed text-muted">
            <ShieldCheck className="mt-0.5 size-4 shrink-0" />
            <p>{t('settings.microphone.localOnly')}</p>
          </div>
        </div>
      </SettingsPanel>
      <p className="text-xs leading-relaxed text-muted">
        {ios ? t('settings.microphone.iosManaged') : t('settings.microphone.localPreference')}
      </p>
    </SettingsPageShell>
  );
};

const VoiceLevelMeter: React.FC<{ level?: VoiceLevelSource | null }> = ({ level }) => (
  <VoiceMeter level={level} mode="live" />
);

export const SettingsMicrophonePage: React.FC = () => {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const ios = isIosDevice();
  const [selection, setSelection] = useState<VoiceInputSelection>(readVoiceInputSelection);
  const [devices, setDevices] = useState<VoiceInputDevice[]>([]);
  const [state, setState] = useState<MicrophoneSettingsState>(() => (
    readVoiceInputSelection() === SYSTEM_DEFAULT_VOICE_INPUT ? 'default' : 'selected'
  ));
  const [missing, setMissing] = useState(false);
  const [level, setLevel] = useState<VoiceLevelSource | null>(null);
  const testRef = useRef<VoiceInputTest | null>(null);
  const acquisition = useRef(0);
  const pending = useRef(false);
  const quietTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let active = true;
    void listVoiceInputDevices()
      .then((next) => {
        if (!active) return;
        setDevices(next);
        if (!ios && selection !== SYSTEM_DEFAULT_VOICE_INPUT) {
          setMissing(!next.some((device) => device.deviceId === selection));
        }
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [ios, selection]);

  const stop = useCallback((interrupted = false) => {
    acquisition.current += 1;
    pending.current = false;
    if (quietTimer.current) clearTimeout(quietTimer.current);
    quietTimer.current = null;
    testRef.current?.stop();
    testRef.current = null;
    setLevel(null);
    setState(interrupted ? 'disconnected' : selection === SYSTEM_DEFAULT_VOICE_INPUT ? 'default' : 'selected');
  }, [selection]);

  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.hidden) stop(true);
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange);
      stop();
    };
  }, [stop]);

  const start = useCallback(async () => {
    if (testRef.current || pending.current) return;
    const request = ++acquisition.current;
    pending.current = true;
    setState('requesting');
    try {
      const test = await startVoiceInputTest({
        onDeviceFallback: () => { if (acquisition.current === request) setMissing(true); },
        onStreamEnded: () => { if (acquisition.current === request) stop(true); },
      });
      if (acquisition.current !== request) {
        test.stop();
        return;
      }
      testRef.current = test;
      setLevel(test.level);
      setState('testing');
      quietTimer.current = setTimeout(() => {
        if (acquisition.current === request && testRef.current === test && test.level.read() === 0) setState('quiet');
      }, 1_500);
      const next = await listVoiceInputDevices().catch(() => []);
      if (acquisition.current !== request) return;
      setDevices(next);
      if (!ios && selection !== SYSTEM_DEFAULT_VOICE_INPUT) {
        setMissing(!next.some((device) => device.deviceId === selection));
      }
    } catch (error) {
      if (acquisition.current !== request) return;
      const name = error instanceof DOMException ? error.name : error instanceof Error ? error.name : '';
      setState(name === 'NotAllowedError' || name === 'SecurityError' ? 'denied' : 'prompt');
      if (name !== 'NotAllowedError' && name !== 'SecurityError') {
        showToast(t('settings.microphone.startFailed'), 'error');
      }
    } finally {
      if (acquisition.current === request) pending.current = false;
    }
  }, [ios, selection, showToast, stop, t]);

  const select = useCallback((next: VoiceInputSelection) => {
    if (!writeVoiceInputSelection(next)) {
      showToast(t('settings.microphone.saveFailed'), 'error');
      return;
    }
    setSelection(next);
    setMissing(false);
    setState(next === SYSTEM_DEFAULT_VOICE_INPUT ? 'default' : 'selected');
  }, [showToast, t]);

  return (
    <MicrophoneSettingsView
      platform={ios ? 'ios' : 'desktop'}
      state={state}
      selection={selection}
      devices={devices}
      missing={missing}
      level={level}
      onSelect={select}
      onStart={() => void start()}
      onStop={() => stop()}
      onPermissionAction={() => void start()}
    />
  );
};
