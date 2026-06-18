import React, { useState } from 'react';
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Copy,
  Key,
  MessageSquare,
  Sparkles,
  Terminal,
  Zap,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import clsx from 'clsx';
import { useApi } from '../../context/ApiContext';
import { useStatus } from '../../context/StatusContext';
import { useToast } from '../../context/ToastContext';
import { copyTextToClipboard } from '../../lib/utils';
import { getEnabledPlatforms } from '../../lib/platforms';
import { withoutConfiguredSecretMarker, withSecretDraft, withSecretDrafts } from '../../lib/secretFields';
import { EyebrowBadge, WizardCard } from '../visual';
import { ToggleSwitch } from '../settings/SettingsPrimitives';
import { Button } from '../ui/button';

interface SummaryProps {
  data: any;
  onNext: (data: any) => void;
  onBack: () => void;
  isFirst: boolean;
  isLast: boolean;
}

// Mirrors design.pen X9wTM (Summary): mint-accented WizardCard with 72×72 check
// halo, 38px title, recap rows, then secondary toggles and quick-start tips.
export const Summary: React.FC<SummaryProps> = ({ data, onBack }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { control } = useStatus();
  const { showToast } = useToast();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [bindCode, setBindCode] = useState<string | null>(null);
  const [codeCopied, setCodeCopied] = useState(false);
  const enabledPlatforms = getEnabledPlatforms(data);
  const discordGuildAllowlist = Array.isArray(data.discordGuildAllowlist)
    ? data.discordGuildAllowlist
    : Array.isArray(data.discord?.guild_allowlist)
      ? data.discord.guild_allowlist
      : [];
  // ``require_mention`` is no longer toggled on the summary (it's configured in
  // Settings); keep the data-derived defaults so buildConfigPayload still
  // persists them. No setter — the value is read-only here.
  const [requireMentionByPlatform] = useState<Record<string, boolean>>(
    Object.fromEntries(
      enabledPlatforms.map((platform) => [
        platform,
        platform === 'discord'
          ? (data.discord?.require_mention || false)
          : platform === 'telegram'
            ? (data.telegram?.require_mention ?? true)
            : platform === 'lark'
              ? (data.lark?.require_mention || false)
              : platform === 'wechat'
                ? (data.wechat?.require_mention || false)
                : (data.slack?.require_mention || false),
      ])
    )
  );
  const [autoUpdate, setAutoUpdate] = useState(data.update?.auto_update ?? true);
  const navigate = useNavigate();

  const copyBindCode = async () => {
    if (!bindCode) return;
    const copied = await copyTextToClipboard(`bind ${bindCode}`);
    if (!copied) {
      showToast(t('common.copyFailed'), 'error');
      return;
    }
    setCodeCopied(true);
    setTimeout(() => setCodeCopied(false), 2000);
  };

  const saveAll = async () => {
    setSaving(true);
    setError(null);
    try {
      const updatedData = {
        ...data,
        // No user-facing primary platform: the backend derives its internal
        // default from ``platforms.enabled``.
        platforms: {
          enabled: enabledPlatforms,
        },
        slack: {
          ...data.slack,
          require_mention: requireMentionByPlatform.slack ?? data.slack?.require_mention,
        },
        discord: {
          ...data.discord,
          require_mention: requireMentionByPlatform.discord ?? data.discord?.require_mention,
        },
        telegram: {
          ...data.telegram,
          require_mention: requireMentionByPlatform.telegram ?? data.telegram?.require_mention,
        },
        lark: {
          ...data.lark,
          require_mention: requireMentionByPlatform.lark ?? data.lark?.require_mention,
        },
        wechat: {
          ...data.wechat,
          require_mention: requireMentionByPlatform.wechat ?? data.wechat?.require_mention,
        },
        update: {
          ...data.update,
          auto_update: autoUpdate,
        },
      };
      const configPayload = buildConfigPayload(updatedData);
      await api.saveConfig(configPayload);
      const settingsByPlatform = buildSettingsPayload(updatedData);
      await Promise.all(
        Object.entries(settingsByPlatform).map(([platform, payload]) => api.saveSettings(payload, platform))
      );

      await control('start');

      // ``enabledPlatforms`` only ever lists real IM platforms — the always-on
      // workbench is stripped by PlatformsConfig.validate() before any config
      // reaches the UI. An empty list is therefore a workbench-only setup.
      if (enabledPlatforms.length === 0) {
        // Workbench-only setup — there is no external bot to bind, so finish
        // instead of falling through to a bogus bind-code flow.
        setSaving(false);
        setTimeout(() => {
          navigate('/');
        }, 1000);
        return;
      }
      if (enabledPlatforms.every((platform) => platform === 'wechat')) {
        setSaving(false);
        showToast(t('wechat.setupComplete'));
        setTimeout(() => {
          navigate('/');
        }, 1000);
        return;
      }

      try {
        const resp = await api.getFirstBindCode();
        if (resp?.code) {
          setBindCode(resp.code);
          setSaving(false);
          return;
        }
      } catch {
        /* non-critical */
      }

      setTimeout(() => {
        navigate('/');
      }, 1000);
    } catch (exc: any) {
      const message = exc && exc.message ? exc.message : 'Failed to save configuration';
      setError(message);
    } finally {
      setSaving(false);
    }
  };

  const recapRows: Array<{ label: string; value: string }> = [
    { label: t('summary.platform'), value: enabledPlatforms.map((p) => titleCase(p)).join(', ') || '—' },
    {
      label: t('summary.enabledAgents'),
      value: enabledAgents(data).map(titleCase).join(', ') || '—',
    },
    {
      label: t('summary.channelsConfigured'),
      value: String(countConfiguredChannels(data.channelConfigsByPlatform)),
    },
  ];

  // Credentials are intentionally NOT echoed back on the summary — the enabled
  // platforms are already listed above, and showing (even masked) tokens just
  // re-exposes secrets. The Discord guild allowlist is a routing setting, not a
  // credential, so it stays.
  if (enabledPlatforms.includes('discord')) {
    recapRows.push({
      label: t('summary.discordGuild'),
      value: discordGuildAllowlist.join(', ') || t('summary.notSet'),
    });
  }

  if (bindCode) {
    return (
      <div className="flex w-full justify-center">
        <WizardCard accent size="hero" className="items-center gap-6 text-center">
          <div className="flex size-[72px] items-center justify-center rounded-full border-2 border-mint/40 bg-mint/[0.08] text-mint shadow-[0_0_48px_-6px_rgba(91,255,160,0.7)]">
            <Check size={36} strokeWidth={2.4} />
          </div>
          <div className="space-y-2">
            <h2 className="text-[28px] font-bold leading-tight tracking-[-0.4px] text-foreground sm:text-[38px] sm:tracking-[-0.7px]">
              {t('summary.title')}
            </h2>
            <p className="mx-auto max-w-[600px] text-[15px] leading-[1.55] text-muted">
              {t('summary.serviceRunning')}
            </p>
          </div>
          <div className="w-full max-w-md rounded-xl border border-gold/30 bg-gold/[0.06] px-5 py-4 text-left">
            <div className="mb-3 flex items-center gap-3">
              <div className="flex size-10 items-center justify-center rounded-lg border border-gold/30 bg-gold/15 text-gold">
                <Key size={18} />
              </div>
              <div>
                <h3 className="text-[14px] font-semibold text-foreground">{t('summary.bindCodeTitle')}</h3>
                <p className="text-[11px] text-muted">{t('summary.bindCodeDesc')}</p>
              </div>
            </div>
            <div className="flex items-center gap-2 rounded-lg border border-border bg-background px-3 py-2.5">
              <code className="min-w-0 flex-1 select-all break-all font-mono text-[13px] text-foreground">bind {bindCode}</code>
              <button
                onClick={copyBindCode}
                className="rounded-md p-1.5 text-muted transition hover:bg-foreground/[0.04] hover:text-foreground"
                title="Copy"
                aria-label={t('common.copy') as string}
              >
                {codeCopied ? <Check size={16} className="text-mint" /> : <Copy size={16} />}
              </button>
            </div>
            {codeCopied && (
              <p className="mt-2 text-[11px] text-mint">{t('summary.bindCodeCopied')}</p>
            )}
          </div>
          <Button variant="brand" size="lg" onClick={() => navigate('/')}>
            {t('summary.goToDashboard')}
            <ArrowRight size={16} strokeWidth={2.25} />
          </Button>
        </WizardCard>
      </div>
    );
  }

  return (
    <div className="flex w-full justify-center">
      <WizardCard accent size="hero" className="gap-6">
        <div className="flex flex-col items-center gap-5 text-center">
          <div className="flex size-[72px] items-center justify-center rounded-full border-2 border-mint/40 bg-mint/[0.08] text-mint shadow-[0_0_48px_-6px_rgba(91,255,160,0.7)]">
            <Check size={36} strokeWidth={2.4} />
          </div>
          <div className="space-y-2">
            <EyebrowBadge tone="mint">{t('summary.eyebrow')}</EyebrowBadge>
            <h2 className="text-[28px] font-bold leading-tight tracking-[-0.4px] text-foreground sm:text-[38px] sm:tracking-[-0.7px]">
              {t('summary.title')}
            </h2>
            <p className="mx-auto max-w-[600px] text-[15px] leading-[1.55] text-muted">
              {t('summary.subtitle')}
            </p>
          </div>
        </div>

        {/* Recap card */}
        <div className="overflow-hidden rounded-xl border border-border bg-background">
          {recapRows.map((row, idx) => (
            <div
              key={row.label}
              className={clsx(
                'flex flex-col gap-1 px-5 py-3.5 sm:flex-row sm:items-center sm:justify-between sm:gap-4',
                idx < recapRows.length - 1 && 'border-b border-border'
              )}
            >
              <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">{row.label}</span>
              <span
                className="min-w-0 break-words font-mono text-[12px] text-foreground sm:truncate"
                title={row.value}
              >
                {row.value}
              </span>
            </div>
          ))}
        </div>

        {/* Auto Update */}
        <div className="flex items-center justify-between rounded-xl border border-border bg-background px-5 py-4">
          <div>
            <h3 className="text-[13px] font-semibold text-foreground">{t('summary.autoUpdate')}</h3>
            <p className="mt-0.5 text-[11px] text-muted">{t('summary.autoUpdateHint')}</p>
          </div>
          <ToggleSwitch enabled={autoUpdate} onClick={() => setAutoUpdate((v: boolean) => !v)} />
        </div>

        {/* Quick start tips */}
        <div className="rounded-xl border border-cyan/30 bg-cyan/[0.05] px-5 py-4">
          <div className="mb-3 flex items-center gap-2">
            <Sparkles size={14} className="text-cyan" />
            <h3 className="text-[13px] font-semibold text-foreground">{t('summary.usageTips')}</h3>
          </div>
          <div className="space-y-3">
            <Tip
              icon={<Terminal size={14} />}
              tone="cyan"
              title={t('summary.tipStartCommand')}
              description={t('summary.tipStartCommandDesc')}
            />
            <Tip
              icon={<Zap size={14} />}
              tone="gold"
              title={t('summary.tipAgentSwitch')}
              description={t('summary.tipAgentSwitchDesc')}
            />
            <Tip
              icon={<MessageSquare size={14} />}
              tone="mint"
              title={t('summary.tipThread')}
              description={t('summary.tipThreadDesc')}
            />
          </div>
        </div>

        {error && (
          <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-[12px] text-danger">
            {error}
          </div>
        )}

        <div className="flex items-center justify-between gap-3 border-t border-border pt-4">
          <Button
            type="button"
            variant="secondary"
            size="default"
            onClick={onBack}
            disabled={saving}
            className="font-semibold"
          >
            <ArrowLeft size={14} strokeWidth={2.25} />
            {t('common.back')}
          </Button>
          <Button type="button" variant="brand" size="lg" onClick={saveAll} disabled={saving} className="flex-1 sm:flex-none">
            {saving ? t('common.saving') : t('summary.finishAndStart')}
            {!saving && <ArrowRight size={16} strokeWidth={2.25} />}
          </Button>
        </div>
      </WizardCard>
    </div>
  );
};

const Tip: React.FC<{
  icon: React.ReactNode;
  tone: 'cyan' | 'gold' | 'mint';
  title: string;
  description: string;
}> = ({ icon, tone, title, description }) => {
  const toneClasses = {
    cyan: 'border-cyan/30 bg-cyan/[0.08] text-cyan',
    gold: 'border-gold/30 bg-gold/10 text-gold',
    mint: 'border-mint/30 bg-mint/[0.08] text-mint',
  }[tone];
  return (
    <div className="flex items-start gap-3">
      <div className={clsx('flex size-8 shrink-0 items-center justify-center rounded-lg border', toneClasses)}>
        {icon}
      </div>
      <div className="min-w-0">
        <p className="text-[12px] font-semibold text-foreground">{title}</p>
        <p className="mt-0.5 text-[11px] leading-[1.5] text-muted">{description}</p>
      </div>
    </div>
  );
};

const titleCase = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);

const enabledAgents = (data: any) => {
  const agents = data.agents || {};
  return Object.keys(agents).filter((name) => agents[name]?.enabled);
};

const countConfiguredChannels = (channelConfigsByPlatform: Record<string, Record<string, any>> = {}) =>
  Object.values(channelConfigsByPlatform).reduce(
    (count, channels) => count + Object.values(channels || {}).filter((config: any) => config?.enabled).length,
    0
  );

const buildConfigPayload = (data: any) => {
  const agents = data.agents || {};
  const enabledPlatforms = getEnabledPlatforms(data);
  return {
    // No user-facing primary platform: the backend derives its internal default
    // from ``platforms.enabled``.
    platforms: {
      enabled: enabledPlatforms,
    },
    mode: data.mode || 'self_host',
    version: 'v2',
    slack: {
      ...withSecretDrafts(data.slack, {
        bot_token: data.slack?.bot_token,
        app_token: data.slack?.app_token,
      }),
      require_mention: data.slack?.require_mention || false,
    },
    discord: {
      ...withSecretDraft(data.discord, 'bot_token', data.discord?.bot_token),
      require_mention: data.discord?.require_mention || false,
    },
    telegram: {
      ...withSecretDraft(data.telegram, 'bot_token', data.telegram?.bot_token),
      require_mention: data.telegram?.require_mention ?? true,
      forum_auto_topic: data.telegram?.forum_auto_topic ?? true,
      use_webhook: data.telegram?.use_webhook ?? false,
    },
    lark: (() => {
      const lark = data.lark || {};
      const appId = lark.app_id || '';
      const appIdChanged = Boolean(lark.original_app_id && appId && appId !== lark.original_app_id);
      const base = appIdChanged ? withoutConfiguredSecretMarker(lark, 'app_secret') : lark;
      return {
        ...withSecretDraft(base, 'app_secret', lark.app_secret),
        app_id: appId,
        domain: lark.domain || 'feishu',
        require_mention: lark.require_mention || false,
      };
    })(),
    wechat: {
      ...withSecretDraft(data.wechat, 'bot_token', data.wechat?.bot_token),
      base_url: data.wechat?.base_url || '',
      require_mention: data.wechat?.require_mention || false,
    },
    runtime: {
      ...data.runtime,
      default_cwd: data.default_cwd || data.runtime?.default_cwd || '_tmp',
    },
    agents: {
      opencode: {
        ...agents.opencode,
        enabled: agents.opencode?.enabled ?? true,
        cli_path: agents.opencode?.cli_path || 'opencode',
        default_agent: data.opencode_default_agent ?? agents.opencode?.default_agent ?? null,
        default_model: data.opencode_default_model ?? agents.opencode?.default_model ?? null,
        default_reasoning_effort:
          data.opencode_default_reasoning_effort ?? agents.opencode?.default_reasoning_effort ?? null,
      },
      claude: {
        ...agents.claude,
        enabled: agents.claude?.enabled ?? true,
        cli_path: agents.claude?.cli_path || 'claude',
        default_model: data.claude_default_model ?? agents.claude?.default_model ?? null,
      },
      codex: {
        ...agents.codex,
        enabled: agents.codex?.enabled ?? false,
        cli_path: agents.codex?.cli_path || 'codex',
        default_model: data.codex_default_model ?? agents.codex?.default_model ?? null,
      },
    },
    gateway: data.gateway,
    ui: {
      ...data.ui,
      setup_host: data.ui?.setup_host || '127.0.0.1',
      setup_port: data.ui?.setup_port || 5123,
    },
    update: data.update
      ? {
          ...data.update,
          auto_update: data.update.auto_update,
        }
      : undefined,
    ack_mode: data.ack_mode,
    show_duration: data.show_duration ?? false,
    language: data.language,
    // Finishing the wizard is the explicit signal that setup is complete. Set
    // here (Summary's own payload, the one saved on Finish) rather than in
    // Wizard.tsx's intermediate-step payload so the flag is only persisted once
    // the user actually reaches and completes the final step (including the
    // Skip → Summary → Finish path).
    setup_completed: true,
  };
};

const buildSettingsPayload = (data: any) => {
  const channelConfigsByPlatform = data.channelConfigsByPlatform || {};
  const discordGuildAllowlist = Array.isArray(data.discordGuildAllowlist)
    ? data.discordGuildAllowlist
    : Array.isArray(data.discord?.guild_allowlist)
      ? data.discord.guild_allowlist
      : [];
  const shouldPersistDiscordGuilds =
    discordGuildAllowlist.length > 0 || data.discordGuildAllowlistTouched === true;
  return Object.fromEntries(
    Object.entries(channelConfigsByPlatform).map(([platform, channels]: any) => [
      platform,
      {
        channels: Object.fromEntries(
          Object.entries(channels || {}).map(([id, cfg]: any) => [
            id,
            {
              enabled: cfg.enabled,
              show_message_types: cfg.show_message_types || [],
              custom_cwd: cfg.custom_cwd || null,
              require_mention: cfg.require_mention ?? null,
              require_bind: cfg.require_bind ?? null,
              routing: {
                agent_name: cfg.routing?.agent_name || null,
                model: cfg.routing?.model || null,
                reasoning_effort: cfg.routing?.reasoning_effort || null,
                opencode_agent: cfg.routing?.opencode_agent || null,
                opencode_model: cfg.routing?.opencode_model || null,
                opencode_reasoning_effort: cfg.routing?.opencode_reasoning_effort || null,
                claude_agent: cfg.routing?.claude_agent || null,
                claude_model: cfg.routing?.claude_model || null,
                claude_reasoning_effort: cfg.routing?.claude_reasoning_effort || null,
                codex_agent: cfg.routing?.codex_agent || null,
                codex_model: cfg.routing?.codex_model || null,
                codex_reasoning_effort: cfg.routing?.codex_reasoning_effort || null,
              },
            },
          ])
        ),
        ...(platform === 'discord' && shouldPersistDiscordGuilds
          ? {
              guilds: Object.fromEntries(
                discordGuildAllowlist.map((guildId: string) => [guildId, { enabled: true }])
              ),
            }
          : {}),
      },
    ])
  );
};
