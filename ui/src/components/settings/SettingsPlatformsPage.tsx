import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { Check, ChevronUp, Loader2, Pencil } from 'lucide-react';

import { useApi } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';
import {
  getEnabledPlatforms,
  getImPlatforms,
  getPlatformCatalog,
  platformHasRunnableConfig,
} from '@/lib/platforms';
import { PlatformIcon } from '@/components/visual';
import { SlackConfig } from '@/components/steps/SlackConfig';
import { DiscordConfig } from '@/components/steps/DiscordConfig';
import { TelegramConfig } from '@/components/steps/TelegramConfig';
import { LarkConfig } from '@/components/steps/LarkConfig';
import { WeChatConfig } from '@/components/steps/WeChatConfig';
import { SettingsPageShell } from './SettingsPageShell';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

const PLATFORM_TILE_STYLES: Record<string, { bg: string; border: string }> = {
  slack: { bg: 'bg-[#4A154B26]', border: 'border-[#4A154B66]' },
  discord: { bg: 'bg-[#5865F226]', border: 'border-[#5865F255]' },
  telegram: { bg: 'bg-[#0088CC26]', border: 'border-[#0088CC55]' },
  lark: { bg: 'bg-[#06A0FB1F]', border: 'border-[#06A0FB55]' },
  feishu: { bg: 'bg-[#06A0FB1F]', border: 'border-[#06A0FB55]' },
  wechat: { bg: 'bg-[#07C16026]', border: 'border-[#07C16055]' },
};

const tileStyle = (id: string) =>
  PLATFORM_TILE_STYLES[id] || { bg: 'bg-foreground/[0.04]', border: 'border-foreground/[0.10]' };

// The platforms settings page is a SINGLE linear flow (no two-step staging):
//  - the "Enabled platforms" grid is always open and is the master control;
//  - checking a tile reveals that platform's config below (frontend-only) when
//    it still needs credentials, or enables it immediately when it is already
//    configured; unchecking disables (if enabled) or just hides the draft card;
//  - nothing is persisted / restarts until a platform's credentials validate
//    and save. There is no user-facing "primary platform" — the backend derives
//    an internal default from the enabled set, so this page never sends one.
export const SettingsPlatformsPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [config, setConfig] = useState<any>(null);
  // Platforms the user revealed to configure but has NOT enabled yet (no creds
  // saved). Frontend-only: these show a config card below but persist nothing
  // until their save validates. Enabled platforms always show a card too.
  const [revealed, setRevealed] = useState<string[]>([]);
  // Which platform's credential form is expanded (the "Configure" toggle).
  const [openConfig, setOpenConfig] = useState<string | null>(null);
  const [busyPlatform, setBusyPlatform] = useState<string | null>(null);
  // The platform pending a disable confirmation (null = no dialog open).
  const [confirmDisableId, setConfirmDisableId] = useState<string | null>(null);
  const [restartPhase, setRestartPhase] = useState<'idle' | 'saving'>('idle');

  useEffect(() => {
    api.getConfig().then(setConfig).catch(() => {});
  }, [api]);

  const platformCatalog = useMemo(() => (config ? getPlatformCatalog(config) : []), [config]);
  // The in-process workbench is always-on and not a togglable IM transport.
  const togglablePlatforms = useMemo(() => (config ? getImPlatforms(config) : []), [config]);
  const enabledPlatforms = useMemo(() => (config ? getEnabledPlatforms(config) : []), [config]);

  // Cards appear for every enabled platform plus any the user revealed to set
  // up, in stable catalog order so the list never reshuffles on toggle.
  const cardPlatforms = useMemo(() => {
    const shown = new Set([...enabledPlatforms, ...revealed]);
    return togglablePlatforms.filter((p) => shown.has(p.id)).map((p) => p.id);
  }, [togglablePlatforms, enabledPlatforms, revealed]);

  const saveConfig = async (nextData: any) => {
    const savedConfig = await api.saveConfig(nextData);
    setConfig(savedConfig);
    return savedConfig;
  };

  const savePlatformSettings = async (platform: string, nextData: any) => {
    const discordGuildAllowlist = nextData?.discordGuildAllowlist;
    if (
      platform === 'discord' &&
      Array.isArray(discordGuildAllowlist) &&
      (discordGuildAllowlist.length > 0 || nextData?.discordGuildAllowlistTouched === true)
    ) {
      await api.saveSettings({
        guilds: Object.fromEntries(
          discordGuildAllowlist.map((guildId: string) => [guildId, { enabled: true }])
        ),
      }, 'discord');
    }
  };

  // Persist the enabled set. ``primary`` is intentionally omitted:
  // the backend normalizes it from ``enabled`` (first enabled, or the workbench
  // when empty), so the UI never chooses or sends one.
  const showApplyResult = (savedConfig: any) => {
    const runtime = savedConfig?.platform_runtime;
    if (runtime?.hot_reconciled) {
      showToast(t('platform.appliedSuccess'), 'success');
      return true;
    }
    if (runtime?.restart_scheduled) {
      showToast(t('platform.restartedSuccess'), 'success');
      return true;
    }
    if (runtime && runtime.hot_reconciled === false) {
      showToast(t('platform.restartFailed'), 'error');
      return false;
    }
    showToast(t('common.saved'), 'success');
    return true;
  };

  const persistEnabled = async (nextEnabled: string[]) => {
    setRestartPhase('saving');
    try {
      try {
        const savedConfig = await saveConfig({ ...config, platforms: { enabled: nextEnabled } });
        showApplyResult(savedConfig);
      } catch {
        showToast(t('common.saveFailed'), 'error');
        return false;
      }
      return true;
    } finally {
      setRestartPhase('idle');
    }
  };

  // Disabling a live platform stops it receiving messages, so confirm first
  // (a misclick on an enabled platform shouldn't silently take it offline).
  const doDisable = async (id: string) => {
    setBusyPlatform(id);
    try {
      await persistEnabled(enabledPlatforms.filter((p) => p !== id));
      setRevealed((prev) => prev.filter((p) => p !== id));
      setOpenConfig((prev) => (prev === id ? null : prev));
    } finally {
      setBusyPlatform(null);
    }
  };

  const toggleTile = async (id: string) => {
    if (busyPlatform) return;
    const enabled = enabledPlatforms.includes(id);
    if (enabled) {
      // Uncheck an enabled platform → confirm before disabling.
      setConfirmDisableId(id);
      return;
    }
    if (revealed.includes(id)) {
      // Uncheck a still-unsaved draft → just hide its card, persist nothing.
      setRevealed((prev) => prev.filter((p) => p !== id));
      setOpenConfig((prev) => (prev === id ? null : prev));
      return;
    }
    if (platformHasRunnableConfig(config, id)) {
      // Already configured → checking enables it immediately.
      setBusyPlatform(id);
      try {
        await persistEnabled([...enabledPlatforms, id]);
      } finally {
        setBusyPlatform(null);
      }
      return;
    }
    // Not configured yet → reveal its config card to enter credentials.
    setRevealed((prev) => (prev.includes(id) ? prev : [...prev, id]));
    setOpenConfig(id);
  };

  // A platform's credential form was saved. Validate-then-enable: persist the
  // credentials, and if they make the platform runnable, add it to the enabled
  // set and restart. An already-enabled platform just re-saves + restarts.
  const handleApplyPlatform = async (platform: string, nextData: any) => {
    const wasEnabled = enabledPlatforms.includes(platform);
    setBusyPlatform(platform);
    setRestartPhase('saving');
    try {
      let savedConfig: any;
      try {
        savedConfig = await saveConfig({ ...nextData, platforms: { enabled: enabledPlatforms } });
      } catch {
        showToast(t('common.saveFailed'), 'error');
        return;
      }
      await savePlatformSettings(platform, nextData);
      const runnable = platformHasRunnableConfig(savedConfig, platform);
      if (!wasEnabled && !runnable) {
        // Saved credentials but they are incomplete — keep the card open so the
        // user can finish; nothing is enabled, no restart.
        showToast(t('common.saved'), 'success');
        return;
      }
      if (wasEnabled) {
        if (showApplyResult(savedConfig)) {
          setRevealed((prev) => prev.filter((p) => p !== platform));
          setOpenConfig((prev) => (prev === platform ? null : prev));
        }
        return;
      }
      const nextEnabled = [...enabledPlatforms, platform];
      try {
        savedConfig = await saveConfig({ ...savedConfig, platforms: { enabled: nextEnabled } });
      } catch {
        showToast(t('platform.restartFailed'), 'error');
        return;
      }
      if (showApplyResult(savedConfig)) {
        setRevealed((prev) => prev.filter((p) => p !== platform));
        setOpenConfig((prev) => (prev === platform ? null : prev));
      }
    } finally {
      setRestartPhase('idle');
      setBusyPlatform(null);
    }
  };

  if (!config) {
    return (
      <SettingsPageShell
        activeTab="platforms"
        title={t('settings.platformsTitle')}
        subtitle={t('settings.platformsSubtitle')}
      >
        <div className="text-sm text-muted">{t('common.loading')}</div>
      </SettingsPageShell>
    );
  }

  return (
    <SettingsPageShell
      activeTab="platforms"
      title={t('settings.platformsTitle')}
      subtitle={t('settings.platformsSubtitle')}
    >
      <div className="mx-auto flex w-full max-w-[920px] flex-col gap-3">
        {restartPhase !== 'idle' && (
          <div
            role="status"
            aria-live="polite"
            className="sticky top-2 z-10 flex items-center gap-3 rounded-xl border border-cyan/35 bg-cyan/[0.08] px-4 py-3 shadow-[0_8px_24px_-8px_rgba(0,212,255,0.35)]"
          >
            <Loader2 size={16} className="shrink-0 animate-spin text-cyan" />
            <div className="min-w-0 flex-1">
              <div className="text-[13px] font-semibold text-foreground">
                {t('platform.applyingConfig')}
              </div>
              <div className="mt-0.5 text-[11px] text-muted">{t('common.saving')}</div>
            </div>
          </div>
        )}

        {/* Enabled platforms — always open, the master control. Check a tile to
            reveal its setup below; nothing persists until credentials save. */}
        <section className="overflow-hidden rounded-xl border border-border bg-surface-2">
          <div className="border-b border-border px-5 py-4">
            <div className="text-[14px] font-semibold text-foreground">{t('platform.enabledPlatforms')}</div>
            <p className="mt-1 text-[12px] leading-relaxed text-muted">{t('platform.subtitle')}</p>
          </div>
          <div className="grid grid-cols-2 gap-2.5 px-5 py-4 md:grid-cols-3 lg:grid-cols-5">
            {togglablePlatforms.map((platform) => {
              const id = platform.id;
              const active = enabledPlatforms.includes(id) || revealed.includes(id);
              const tile = tileStyle(id);
              const busy = busyPlatform === id;
              const otherBusy = !!busyPlatform && !busy;
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => void toggleTile(id)}
                  disabled={busy || otherBusy}
                  aria-pressed={active}
                  className={clsx(
                    'relative flex flex-col items-center gap-2 rounded-xl px-3 py-3.5 transition-colors',
                    active
                      ? 'border-2 border-mint bg-mint/[0.16]'
                      : 'border border-foreground/[0.08] bg-background hover:border-foreground/[0.16] hover:bg-foreground/[0.02]',
                    otherBusy && 'opacity-50'
                  )}
                >
                  {active && (
                    <span className="absolute right-1.5 top-1.5 inline-flex size-4 items-center justify-center rounded-full bg-mint text-background">
                      {busy ? <Loader2 size={10} className="animate-spin" /> : <Check size={11} strokeWidth={3} />}
                    </span>
                  )}
                  <span
                    className={clsx(
                      'inline-flex size-9 items-center justify-center rounded-[10px] border',
                      tile.bg,
                      tile.border
                    )}
                  >
                    <PlatformIcon platform={id as any} size={18} />
                  </span>
                  <span
                    className={clsx(
                      'text-[12px] leading-tight transition-colors',
                      active ? 'font-bold text-foreground' : 'font-medium text-muted'
                    )}
                  >
                    {t(platform.title_key || `platform.${id}.title`)}
                  </span>
                </button>
              );
            })}
          </div>
        </section>

        {/* One config card per enabled-or-revealed platform. Disabled platforms
            with no credentials only appear here after the user checks them. */}
        {cardPlatforms.map((id) => {
          const descriptor = platformCatalog.find((p) => p.id === id);
          const label = t(descriptor?.title_key || `platform.${id}.title`);
          const description = t(descriptor?.description_key || `platform.${id}.desc`);
          const tile = tileStyle(id);
          const runnable = platformHasRunnableConfig(config, id);
          const enabled = enabledPlatforms.includes(id);
          // ``toggleTile`` opens a freshly-revealed platform's form (so settings
          // appear right when you check it); from then on the Configure/Close
          // toggle owns the state, so Close actually collapses the card.
          const open = openConfig === id;
          return (
            <PlatformCard
              key={id}
              expanded={open}
              onToggle={() => setOpenConfig((prev) => (prev === id ? null : id))}
              header={
                <div className="flex min-w-0 flex-1 items-center gap-3">
                  <span
                    className={clsx(
                      'inline-flex size-9 shrink-0 items-center justify-center rounded-[10px] border',
                      tile.bg,
                      tile.border
                    )}
                  >
                    <PlatformIcon platform={id as any} size={18} />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-[14px] font-semibold text-foreground">{label}</span>
                      {runnable ? (
                        <span className="inline-flex items-center gap-1 rounded border border-mint/30 bg-mint/[0.08] px-1.5 py-0.5 text-[10px] font-medium text-mint">
                          <Check size={10} />
                          {t('platform.validationSuccess')}
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 rounded border border-gold/30 bg-gold/10 px-1.5 py-0.5 text-[10px] font-medium text-gold">
                          {t('platform.stepAddBotToken')}
                        </span>
                      )}
                      {enabled && (
                        <Badge variant="success" className="px-1.5 py-0 text-[10px]">
                          {t('common.enabled')}
                        </Badge>
                      )}
                    </div>
                    <p className="mt-0.5 truncate text-[11px] text-muted">{description}</p>
                  </div>
                </div>
              }
            >
              <div className="px-5 py-4">
                <PlatformConfigEmbed
                  platform={id}
                  config={config}
                  onApply={(data) => handleApplyPlatform(id, data)}
                  onCancel={() => setOpenConfig((prev) => (prev === id ? null : prev))}
                />
              </div>
            </PlatformCard>
          );
        })}
      </div>

      <Dialog open={confirmDisableId !== null} onOpenChange={(open) => !open && setConfirmDisableId(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('platform.disableConfirmTitle')}</DialogTitle>
            <DialogDescription>
              {t('platform.disableConfirmBody', {
                name: confirmDisableId
                  ? t(
                      platformCatalog.find((p) => p.id === confirmDisableId)?.title_key ||
                        `platform.${confirmDisableId}.title`
                    )
                  : '',
              })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="secondary" size="sm" onClick={() => setConfirmDisableId(null)}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="destructive-soft"
              size="sm"
              onClick={() => {
                const id = confirmDisableId;
                setConfirmDisableId(null);
                if (id) void doDisable(id);
              }}
            >
              {t('platform.disableConfirmCta')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </SettingsPageShell>
  );
};

const PlatformCard: React.FC<{
  expanded: boolean;
  onToggle: () => void;
  header: React.ReactNode;
  children: React.ReactNode;
}> = ({ expanded, onToggle, header, children }) => {
  const { t } = useTranslation();
  return (
    <section
      className={clsx(
        'overflow-hidden rounded-xl border bg-surface-2 transition-colors',
        expanded ? 'border-mint/35 shadow-[0_8px_32px_-8px_rgba(91,255,160,0.078)]' : 'border-border'
      )}
    >
      <div className="flex items-stretch gap-3 px-5 py-4">
        {header}
        <button
          type="button"
          onClick={onToggle}
          className={clsx(
            'inline-flex shrink-0 items-center gap-1.5 rounded-lg border px-3 py-1.5 text-[12px] font-medium transition',
            expanded
              ? 'border-mint/35 bg-mint/[0.08] text-mint'
              : 'border-border bg-foreground/[0.04] text-foreground hover:border-border-strong'
          )}
        >
          {expanded ? (
            <>
              <ChevronUp size={14} />
              {t('common.close')}
            </>
          ) : (
            <>
              <Pencil size={12} />
              {t('common.configure')}
            </>
          )}
        </button>
      </div>
      {expanded && <div className="border-t border-border bg-background/40">{children}</div>}
    </section>
  );
};

const PlatformConfigEmbed: React.FC<{
  platform: string;
  config: any;
  onApply: (data: any) => Promise<void>;
  onCancel: () => void;
}> = ({ platform, config, onApply, onCancel }) => {
  const noopNext = () => {};
  if (platform === 'slack') {
    return <SlackConfig data={config} onNext={noopNext} embedded onApply={onApply} onCancel={onCancel} />;
  }
  if (platform === 'discord') {
    return <DiscordConfig data={config} onNext={noopNext} embedded onApply={onApply} onCancel={onCancel} />;
  }
  if (platform === 'telegram') {
    return <TelegramConfig data={config} onNext={noopNext} embedded onApply={onApply} onCancel={onCancel} />;
  }
  if (platform === 'lark') {
    return <LarkConfig data={config} onNext={noopNext} embedded onApply={onApply} onCancel={onCancel} />;
  }
  if (platform === 'wechat') {
    return (
      <WeChatConfig
        data={config}
        onNext={noopNext}
        embedded
        onApply={onApply}
        onCancel={onCancel}
        autoStartLogin={false}
      />
    );
  }
  return null;
};
