import React from 'react';
import { useTranslation } from 'react-i18next';

import { desktopRemoteInstallActions, isDesktopShell, type DesktopRemoteInstallActions } from '../../lib/desktopShell';
import { SettingsPanel, ToggleSwitch } from './SettingsPrimitives';
import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../ui/dialog';

/**
 * A local-shell-only preference. The Runtime and remote browsers may report
 * the resulting value, but only this Workbench window can read or write it.
 */
export const DesktopRemoteUpdateSetting: React.FC = () => {
  const { t } = useTranslation();
  const [actions, setActions] = React.useState<DesktopRemoteInstallActions | null>(null);
  const [enabled, setEnabled] = React.useState<boolean | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [confirming, setConfirming] = React.useState(false);
  const [saveFailed, setSaveFailed] = React.useState(false);

  React.useEffect(() => {
    if (!isDesktopShell()) return;
    const candidate = desktopRemoteInstallActions();
    if (!candidate) return;
    let active = true;
    void candidate.get().then((value) => {
      if (!active) return;
      setActions(candidate);
      setEnabled(value);
    }).catch(() => {
      // Unknown commands are the compatibility signal for an older shell.
      if (active) {
        setActions(null);
        setEnabled(null);
      }
    });
    return () => {
      active = false;
    };
  }, []);

  if (!actions || enabled === null) return null;

  const handleChange = async (next: boolean) => {
    if (saving) return;
    setSaving(true);
    setSaveFailed(false);
    try {
      const saved = await actions.set(next);
      setEnabled(saved);
      setConfirming(false);
    } catch {
      setSaveFailed(true);
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
    <SettingsPanel
      variant="preference"
      title={t('settings.general.remoteInstallTitle')}
      description={t('settings.general.remoteInstallDescription')}
      actions={(
        <ToggleSwitch
          enabled={enabled}
          onClick={() => enabled ? void handleChange(false) : setConfirming(true)}
          disabled={saving}
          label={t('settings.general.remoteInstallTitle')}
        />
      )}
    >
      {enabled && (
        <p className="rounded-md border border-gold/30 bg-gold/10 px-3 py-2 text-[12px] leading-relaxed text-gold-ink">
          {t('settings.general.remoteInstallWarning')}
        </p>
      )}
      {saveFailed && !confirming && <p role="alert" className="text-sm text-destructive-ink">{t('settings.general.remoteInstallSaveFailed')}</p>}
    </SettingsPanel>
    <Dialog open={confirming} onOpenChange={(open) => { if (!saving) setConfirming(open); }}>
      <DialogContent closeLabel={t('common.close')}>
        <DialogHeader>
          <DialogTitle>{t('settings.general.remoteInstallTitle')}</DialogTitle>
          <DialogDescription>{t('settings.general.remoteInstallWarning')}</DialogDescription>
        </DialogHeader>
        {saveFailed && <p role="alert" className="text-sm text-destructive-ink">{t('settings.general.remoteInstallSaveFailed')}</p>}
        <DialogFooter>
          <Button variant="ghost" disabled={saving} onClick={() => setConfirming(false)}>{t('common.cancel')}</Button>
          <Button variant="brand" disabled={saving} onClick={() => void handleChange(true)}>{t('settings.general.remoteInstallEnable')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
    </>
  );
};
