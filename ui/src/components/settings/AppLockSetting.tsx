import React from 'react';
import { useTranslation } from 'react-i18next';
import { Info, KeyRound, Loader2, ShieldCheck, TriangleAlert } from 'lucide-react';

import { appLockActions, type AppLockActions, type AppLockDevice } from '../../lib/appLock';
import type { AppLockPolicy, AppLockStatus } from '../../lib/appLockContract';
import { cn } from '../../lib/utils';
import { Select } from '../ui/select';
import { SettingsPanel } from './SettingsPrimitives';

/** What the panel says under its choice while it is not idle. */
export type AppLockSettingActivity = 'idle' | 'verifying' | 'failed';

/**
 * App Lock, a per-device preference the shell owns (ui/design: app-lock/settings/*). The shell
 * saves a change only after the device owner verifies, so the choice shown is always the
 * shell's answer, never an optimistic one.
 */
export const AppLockSetting: React.FC = () => {
  const [actions] = React.useState<AppLockActions | null>(() => appLockActions());
  const [status, setStatus] = React.useState<AppLockStatus | null>(null);
  const [activity, setActivity] = React.useState<AppLockSettingActivity>('idle');

  // Bumped by every change: a read that started before it must not undo its answer.
  const changes = React.useRef(0);

  React.useEffect(() => {
    if (!actions) return;
    let active = true;
    const read = () => {
      const generation = changes.current;
      void actions.status().then((value) => {
        if (active && generation === changes.current) setStatus(value);
      }).catch(() => {
        // A shell that cannot answer keeps the panel hidden, as a browser does.
        if (active && generation === changes.current) setStatus(null);
      });
    };
    // A passcode or Face ID permission changes in system settings, outside the page:
    // read again whenever the page comes back.
    const onVisible = () => {
      if (document.visibilityState === 'visible') read();
    };
    read();
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('focus', read);
    return () => {
      active = false;
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('focus', read);
    };
  }, [actions]);

  if (!actions || !status) return null;

  const change = async (policy: AppLockPolicy) => {
    if (activity === 'verifying' || policy === status.policy) return;
    changes.current += 1;
    setActivity('verifying');
    try {
      const result = await actions.setPolicy(policy);
      setStatus(result.status);
      // `unavailable` is explained by the status itself (no passcode).
      setActivity(result.ok || result.reason === 'unavailable' ? 'idle' : 'failed');
    } catch {
      setActivity('failed');
    }
  };

  return (
    <AppLockSettingView
      device={actions.device}
      status={status}
      activity={activity}
      onChange={(policy) => void change(policy)}
    />
  );
};

export const AppLockSettingView: React.FC<{
  device: AppLockDevice;
  status: AppLockStatus;
  activity?: AppLockSettingActivity;
  onChange?: (policy: AppLockPolicy) => void;
}> = ({ device, status, activity = 'idle', onChange }) => {
  const { t } = useTranslation();
  const title = t('settings.general.appLock.title');
  const description = device === 'mac'
    ? t('settings.general.appLock.descriptionMac')
    : status.biometry === 'touchID'
      ? t('settings.general.appLock.descriptionTouchIdPhone')
      : t('settings.general.appLock.descriptionFaceId');
  const options: [AppLockPolicy, string][] = [
    ['off', t('settings.general.appLock.off')],
    ['immediately', t('settings.general.appLock.immediately')],
    ['oneMinute', t('settings.general.appLock.oneMinute')],
    ['fiveMinutes', t('settings.general.appLock.fiveMinutes')],
  ];
  // Without a passcode the lock does not engage, so the choice reads as what is in effect.
  const shown = status.available ? status.policy : 'off';

  return (
    <SettingsPanel
      variant="preference"
      title={title}
      description={description}
      actions={(
        <Select
          aria-label={title}
          value={shown}
          disabled={activity === 'verifying' || !status.available}
          onChange={(event) => onChange?.(event.target.value as AppLockPolicy)}
          wrapperClassName="w-[190px] max-w-full"
          className="h-10 rounded-[9px] px-3.5 pr-9 text-[13px]"
        >
          {options.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </Select>
      )}
    >
      <AppLockNote device={device} status={status} activity={activity} />
    </SettingsPanel>
  );
};

function Note({ tone, icon, children }: { tone: 'muted' | 'warn' | 'error'; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <p
      role={tone === 'error' ? 'alert' : undefined}
      className={cn(
        'mt-3 flex items-start gap-2 text-[12px] leading-relaxed',
        tone === 'muted' && 'text-muted',
        tone === 'warn' && 'rounded-md border border-gold/30 bg-gold/10 px-3 py-2 text-gold-ink',
        tone === 'error' && 'text-destructive-ink',
      )}
    >
      <span className="mt-[2px] shrink-0 [&_svg]:size-[14px]">{icon}</span>
      <span>{children}</span>
    </p>
  );
}

function AppLockNote({ device, status, activity }: { device: AppLockDevice; status: AppLockStatus; activity: AppLockSettingActivity }) {
  const { t } = useTranslation();
  if (activity === 'verifying') {
    return <Note tone="muted" icon={<Loader2 className="animate-spin" />}>{t('settings.general.appLock.verifying')}</Note>;
  }
  if (activity === 'failed') {
    return <Note tone="error" icon={<TriangleAlert />}>{t('settings.general.appLock.failed')}</Note>;
  }
  if (!status.available) {
    return (
      <Note tone="warn" icon={<Info />}>
        {device === 'mac' ? t('settings.general.appLock.noPasswordMac') : t('settings.general.appLock.noPasscodePhone')}
      </Note>
    );
  }
  if (device === 'iphone' && status.biometry === 'faceID' && status.biometryState === 'denied') {
    return <Note tone="muted" icon={<KeyRound />}>{t('settings.general.appLock.faceIdDenied')}</Note>;
  }
  if (device === 'mac' && status.biometryState !== 'usable' && status.biometryState !== 'lockout') {
    return <Note tone="muted" icon={<KeyRound />}>{t('settings.general.appLock.passwordOnlyMac')}</Note>;
  }
  if (status.policy === 'off') return null;
  return (
    <Note tone="muted" icon={<ShieldCheck />}>
      {device === 'mac' ? t('settings.general.appLock.coversMac') : t('settings.general.appLock.coversPhone')}
    </Note>
  );
}
