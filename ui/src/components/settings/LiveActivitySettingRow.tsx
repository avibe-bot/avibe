import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  hasNativeCapability, nativeLiveActivitySetting, nativeLiveActivitySetSetting,
  nativeLiveActivityOpenSettings, type NativeLiveActivitySetting,
} from '@/lib/native';
import { onPageReactivated } from '@/lib/pageActivity';
import { Button } from '../ui/button';
import { SettingsRow, ToggleSwitch } from './SettingsPrimitives';

/** ui/design: settings/live-activity/off, on, system-off (device-local, owned by the App). */
export function LiveActivitySettingRow() {
  const { t } = useTranslation();
  const supported = hasNativeCapability('lockScreenProgress');
  const [setting, setSetting] = useState<NativeLiveActivitySetting | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const mounted = useRef(false);
  const saving = useRef(false);
  const request = useRef(0);
  const refresh = useCallback(() => {
    if (!supported || saving.current) return;
    const id = ++request.current;
    return nativeLiveActivitySetting().then((result) => {
      if (mounted.current && id === request.current) {
        setSetting(result);
        setFailed(result === null);
      }
    }).catch(() => {
      if (mounted.current && id === request.current) { setSetting(null); setFailed(true); }
    });
  }, [supported]);
  useEffect(() => {
    mounted.current = true;
    void refresh();
    const unsubscribe = onPageReactivated(() => void refresh());
    return () => {
      mounted.current = false;
      ++request.current;
      unsubscribe();
    };
  }, [refresh]);
  const save = async () => {
    if (!setting?.systemEnabled || saving.current) return;
    saving.current = true;
    ++request.current;
    setBusy(true);
    setFailed(false);
    try {
      const result = await nativeLiveActivitySetSetting(!setting.enabled);
      if (mounted.current) {
        if (result) setSetting(result);
        setFailed(result === null);
      }
    } catch {
      if (mounted.current) setFailed(true);
    } finally {
      saving.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  if (!supported) return null;
  const systemOff = setting !== null && !setting.systemEnabled;
  return <SettingsRow
    title={t('liveActivity.title')}
    description={<>
      <span>{t(systemOff ? 'liveActivity.systemOff' : 'liveActivity.description')}</span>
      {failed && (setting ? <div role="alert">{t('common.saveFailed')}</div> :
        <Button variant="ghost" size="sm" onClick={() => void refresh()}>{t('common.retry')}</Button>)}
    </>}
    control={<div className="flex items-center gap-3">
      {systemOff && <Button type="button" variant="outline" size="xs" onClick={() => {
        void nativeLiveActivityOpenSettings().catch(() => { if (mounted.current) setFailed(true); });
      }}>{t('liveActivity.settings')}</Button>}
      <ToggleSwitch enabled={setting?.enabled ?? false} disabled={!setting?.systemEnabled || busy}
        label={t('liveActivity.title')} onClick={() => void save()} />
    </div>}
  />;
}
