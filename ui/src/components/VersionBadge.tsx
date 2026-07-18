import React from 'react';
import { useTranslation } from 'react-i18next';
import { useApi, type VersionInfo, type UpgradeResult } from '../context/ApiContext';
import { Download, X, RefreshCw, Check, AlertCircle, GitCommitHorizontal } from 'lucide-react';
import clsx from 'clsx';
import { ToggleSwitch } from './settings/SettingsPrimitives';
import { Button } from './ui/button';
import { badgeVariants } from './ui/badge';
import { cn } from '@/lib/utils';
import { scheduleUpgradeReload } from '../lib/upgradeReload';

// Dev / regression builds carry long versions like
// `3.0.1.dev33+g1df6865a1.d20260608`. Collapse to the release core
// (major.minor.patch) + a short tail, dropping the build metadata in between,
// so the badge stays tiny — e.g. `3.0.1…0608`. The full string is still
// available via the trigger's title tooltip and the popup's current-version row.
function shortenVersion(value: string, tail = 4): string {
  const core = value.match(/^\d+\.\d+\.\d+/)?.[0] ?? value;
  if (value.length <= core.length + 1 + tail) return value;
  return `${core}…${value.slice(-tail)}`;
}

export const VersionBadge: React.FC<{ openUpward?: boolean }> = ({ openUpward = false }) => {
  const { t } = useTranslation();
  const api = useApi();
  const [versionInfo, setVersionInfo] = React.useState<VersionInfo | null>(null);
  const [isPopupOpen, setIsPopupOpen] = React.useState(false);
  const [checking, setChecking] = React.useState(false);
  const [upgrading, setUpgrading] = React.useState(false);
  const [restarting, setRestarting] = React.useState(false);
  const [upgradeResult, setUpgradeResult] = React.useState<UpgradeResult | null>(null);
  const [autoUpdate, setAutoUpdate] = React.useState<boolean | null>(null);
  const [savingAutoUpdate, setSavingAutoUpdate] = React.useState(false);
  const popupRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    checkVersion();
    loadAutoUpdateSetting();
  }, []);

  React.useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (popupRef.current && !popupRef.current.contains(event.target as Node)) {
        setIsPopupOpen(false);
      }
    };
    if (isPopupOpen) {
      document.addEventListener('mousedown', handleClickOutside);
    }
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [isPopupOpen]);

  const loadAutoUpdateSetting = async () => {
    try {
      const config = await api.getConfig();
      setAutoUpdate(config.update?.auto_update ?? true);
    } catch (e) {
      console.error('Failed to load config:', e);
    }
  };

  const handleAutoUpdateToggle = async (enabled: boolean) => {
    setSavingAutoUpdate(true);
    try {
      await api.saveConfig({ update: { auto_update: enabled } });
      setAutoUpdate(enabled);
    } catch (e) {
      console.error('Failed to save auto-update setting:', e);
    } finally {
      setSavingAutoUpdate(false);
    }
  };

  const checkVersion = async () => {
    setChecking(true);
    try {
      const info = await api.getVersion();
      setVersionInfo(info);
    } catch (e) {
      console.error('Failed to check version:', e);
    } finally {
      setChecking(false);
    }
  };

  const handleUpgrade = async () => {
    setUpgrading(true);
    setUpgradeResult(null);
    try {
      const result = await api.doUpgrade();
      setUpgradeResult(result);
      if (result.ok) {
        if (result.restarting) {
          setRestarting(true);
          scheduleUpgradeReload(() => {
            window.location.reload();
          });
        } else {
          setTimeout(() => checkVersion(), 1000);
        }
      }
    } catch (e) {
      setUpgradeResult({ ok: false, message: String(e), output: null, restarting: false });
    } finally {
      setUpgrading(false);
    }
  };

  const hasUpdate = versionInfo?.has_update === true;
  const currentVersion = versionInfo?.current || '...';
  const isSourceBuild = versionInfo?.build?.kind === 'source';
  const sourceRevision = versionInfo?.build?.revision;
  const shortSourceRevision = sourceRevision?.slice(0, 12) || t('dashboard.unknownRevision');
  const displayVersion = isSourceBuild ? shortSourceRevision : shortenVersion(currentVersion);
  const refreshLabel = checking
    ? t('dashboard.checking')
    : t(isSourceBuild ? 'dashboard.refreshBuildInfo' : 'dashboard.checkUpdate');
  const badgeTitle = isSourceBuild
    ? `${t('dashboard.sourceRevision')}: ${sourceRevision || t('dashboard.unknownRevision')}${versionInfo?.build?.dirty ? ` (${t('dashboard.dirtySource')})` : ''}`
    : `v${currentVersion}`;

  return (
    <div className="relative" ref={popupRef}>
      {/* Version Badge trigger */}
      <button
        type="button"
        onClick={() => setIsPopupOpen(!isPopupOpen)}
        className={cn(
          badgeVariants({ variant: hasUpdate ? 'warning' : 'secondary' }),
          'relative cursor-pointer rounded-md font-medium tracking-normal hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
        )}
        title={badgeTitle}
      >
        {isSourceBuild ? <GitCommitHorizontal size={12} /> : 'v'}
        {displayVersion}
        {hasUpdate && (
          <span className="absolute -top-1 -right-1 size-2.5 rounded-full border-2 border-background bg-gold animate-pulse" />
        )}
      </button>

      {/* Popup */}
      {isPopupOpen && (
        <div
          className={clsx(
            'z-50 rounded-lg border border-border bg-popover text-popover-foreground shadow-xl',
            // Mobile: full-width fixed below sticky header, with scroll
            'fixed inset-x-3 top-[4.5rem] max-h-[calc(100dvh-5.5rem)] overflow-auto',
            // Desktop: anchor to trigger, fixed width
            'md:absolute md:inset-x-auto md:max-h-none md:w-72 md:overflow-visible',
            openUpward
              ? 'md:bottom-full md:left-0 md:top-auto md:mb-2'
              : 'md:left-0 md:top-full md:mt-2'
          )}
        >
          {/* Header */}
          <div className="flex items-center justify-between border-b border-border px-4 py-3">
            <span className="text-sm font-medium text-foreground">
              {t(isSourceBuild ? 'dashboard.buildAndVersion' : 'dashboard.versionAndUpdate')}
            </span>
            <div className="flex items-center gap-1">
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 text-muted hover:text-foreground"
                onClick={checkVersion}
                disabled={checking || restarting}
                aria-label={refreshLabel}
                title={refreshLabel}
              >
                <RefreshCw size={14} className={checking ? 'animate-spin' : ''} />
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 text-muted hover:text-foreground"
                onClick={() => setIsPopupOpen(false)}
                aria-label={t('common.close')}
              >
                <X size={14} />
              </Button>
            </div>
          </div>

          {/* Content */}
          <div className="space-y-3 p-4">
            {isSourceBuild && (
              <div className="flex items-start justify-between gap-3 text-sm">
                <span className="shrink-0 text-muted">{t('dashboard.sourceRevision')}</span>
                <span className="max-w-[10rem] break-all text-right font-mono font-medium text-foreground">
                  {sourceRevision || t('dashboard.unknownRevision')}
                  {versionInfo?.build?.dirty ? ` (${t('dashboard.dirtySource')})` : ''}
                </span>
              </div>
            )}

            {/* Current package version */}
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted">
                {t(isSourceBuild ? 'dashboard.packageVersion' : 'dashboard.currentVersion')}
              </span>
              <span className="max-w-[10rem] break-all text-right font-mono font-medium text-foreground">{currentVersion}</span>
            </div>

            {/* Latest Version */}
            {!isSourceBuild && versionInfo?.latest && (
              <div className="flex items-center justify-between text-sm">
                <span className="text-muted">{t('dashboard.latestVersion')}</span>
                <span className="font-mono font-medium text-foreground">{versionInfo.latest}</span>
              </div>
            )}

            {/* Update Status */}
            {!isSourceBuild && (hasUpdate ? (
              <div className="flex items-center gap-2 rounded-md border border-gold/30 bg-gold/10 px-3 py-2 text-sm text-gold">
                <AlertCircle size={16} className="shrink-0" />
                <span>
                  {t('dashboard.updateHint', {
                    from: currentVersion,
                    to: versionInfo?.latest,
                  })}
                </span>
              </div>
            ) : versionInfo && !versionInfo.error ? (
              <div className="flex items-center gap-2 rounded-md border border-mint/25 bg-mint/10 px-3 py-2 text-sm text-mint">
                <Check size={16} className="shrink-0" />
                <span>{t('dashboard.upToDate')}</span>
              </div>
            ) : versionInfo?.error ? (
              <div className="flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                <AlertCircle size={16} className="shrink-0" />
                <span>{t('dashboard.checkFailed')}</span>
              </div>
            ) : null)}

            {/* Upgrade Result */}
            {upgradeResult && (
              <div
                className={clsx(
                  'flex items-center gap-2 rounded-md border px-3 py-2 text-sm',
                  upgradeResult.ok
                    ? 'border-mint/25 bg-mint/10 text-mint'
                    : 'border-destructive/30 bg-destructive/10 text-destructive'
                )}
              >
                {upgradeResult.ok ? <Check size={16} className="shrink-0" /> : <AlertCircle size={16} className="shrink-0" />}
                <span>
                  {upgradeResult.ok ? t('dashboard.upgradeSuccess') : t('dashboard.upgradeFailed')}
                </span>
              </div>
            )}

            {/* Restarting Status */}
            {restarting && (
              <div className="flex items-center gap-2 rounded-md border border-cyan/30 bg-cyan/10 px-3 py-2 text-sm text-cyan">
                <RefreshCw size={16} className="shrink-0 animate-spin" />
                <span>{t('dashboard.restarting')}</span>
              </div>
            )}

            {/* Auto Update Toggle */}
            {!isSourceBuild && autoUpdate !== null && (
              <div className="flex items-center justify-between gap-3 border-t border-border pt-3">
                <div className="min-w-0">
                  <div className="text-sm text-foreground">{t('dashboard.autoUpdate')}</div>
                  <div className="text-xs text-muted">{t('dashboard.autoUpdateHint')}</div>
                </div>
                <ToggleSwitch
                  enabled={autoUpdate}
                  onClick={() => handleAutoUpdateToggle(!autoUpdate)}
                  disabled={savingAutoUpdate}
                />

              </div>
            )}
          </div>

          {/* Actions */}
          {hasUpdate && !restarting && (
            <div className="flex justify-end border-t border-border px-4 py-3">
              <Button variant="brand" size="xs" onClick={handleUpgrade} disabled={upgrading}>
                <Download size={14} className={upgrading ? 'animate-bounce' : ''} />
                {upgrading ? t('dashboard.upgrading') : t('dashboard.upgradeNow')}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
