import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { ArrowLeft, ArrowRight, Check, Circle, CircleCheckBig, ExternalLink, Loader2, Lock, Plus, Smartphone } from 'lucide-react';
import { useApi } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import {
  getEnabledPlatforms,
  getImPlatforms,
  getPlatformCatalog,
  WORKBENCH_PLATFORM_ID,
} from '../../lib/platforms';
import { hasUsableSecret, secretInputValue } from '../../lib/secretFields';
import { EyebrowBadge, PlatformIcon, WizardCard } from '../visual';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { Select } from '../ui/select';

// Per-platform brand-tinted tile container colors (matches design.pen wT*L
// frames: Slack purple-soft, Discord indigo-soft, etc.).
const PLATFORM_TILE_STYLES: Record<string, { bg: string; border: string }> = {
  slack: { bg: 'bg-[#4A154B26]', border: 'border-[#4A154B66]' },
  discord: { bg: 'bg-[#5865F226]', border: 'border-[#5865F255]' },
  telegram: { bg: 'bg-[#0088CC26]', border: 'border-[#0088CC55]' },
  lark: { bg: 'bg-[#06A0FB1F]', border: 'border-[#06A0FB55]' },
  feishu: { bg: 'bg-[#06A0FB1F]', border: 'border-[#06A0FB55]' },
  wechat: { bg: 'bg-[#07C16026]', border: 'border-[#07C16055]' },
};

interface PlatformSelectionProps {
  data: any;
  onNext: (data: any) => void;
  onBack?: () => void;
  isPage?: boolean;
  onSave?: (data: any) => Promise<void> | void;
}

type ValidationState = 'idle' | 'success' | 'error';

const buildInitialCredentialDraft = (data: any) => ({
  slack: {
    ...(data.slack || {}),
    bot_token: secretInputValue(data.slack, 'bot_token') || data.slackBotToken || '',
    app_token: secretInputValue(data.slack, 'app_token') || data.slackAppToken || '',
  },
  discord: {
    ...(data.discord || {}),
    bot_token: secretInputValue(data.discord, 'bot_token'),
    client_id: data.discord?.client_id || data.discord_client_id || '',
  },
  telegram: {
    require_mention: true,
    forum_auto_topic: true,
    ...(data.telegram || {}),
    bot_token: secretInputValue(data.telegram, 'bot_token'),
  },
  lark: {
    domain: 'feishu',
    ...(data.lark || {}),
    app_id: data.lark?.app_id || '',
    original_app_id: data.lark?.original_app_id || data.lark?.app_id || '',
    app_secret: secretInputValue(data.lark, 'app_secret'),
  },
  wechat: {
    ...(data.wechat || {}),
  },
});

export const PlatformSelection: React.FC<PlatformSelectionProps> = ({ data, onNext, onBack, isPage = false, onSave }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  // The Avibe Workbench card is purely presentational — it is not part of the
  // selectable IM set. ``selected`` / ``platforms.enabled`` tracks only the
  // optional third-party chat platforms (and may be empty for a workbench-only
  // setup).
  const initialPlatforms = useMemo(() => getEnabledPlatforms(data), [data]);
  const platformCatalog = useMemo(() => getPlatformCatalog(data), [data]);
  // The credential-tab fallback must be a configurable IM platform — never the
  // always-on workbench (which has no credentials and isn't in the selectable
  // grid), so default to the first IM platform rather than the full catalog.
  const defaultCredentialPlatform = useMemo(() => getImPlatforms(data)[0]?.id || 'slack', [data]);
  const [selected, setSelected] = useState<string[]>(initialPlatforms);
  const [activeCredentialPlatform, setActiveCredentialPlatform] = useState<string>(
    initialPlatforms[0] || defaultCredentialPlatform
  );
  const [credentialDraft, setCredentialDraft] = useState<Record<string, any>>(() => buildInitialCredentialDraft(data));
  const [validating, setValidating] = useState(false);
  const [validationState, setValidationState] = useState<Record<string, ValidationState>>({});

  useEffect(() => {
    if (!selected.includes(activeCredentialPlatform)) {
      setActiveCredentialPlatform(selected[0] || defaultCredentialPlatform);
    }
  }, [activeCredentialPlatform, defaultCredentialPlatform, selected]);

  const togglePlatform = (platform: string) => {
    setSelected((current) => {
      if (current.includes(platform)) {
        return current.filter((item) => item !== platform);
      }
      setActiveCredentialPlatform(platform);
      return [...current, platform];
    });
  };

  const updateCredential = (platform: string, patch: Record<string, any>) => {
    setCredentialDraft((current) => ({
      ...current,
      [platform]: {
        ...(current[platform] || {}),
        ...patch,
      },
    }));
    setValidationState((current) => ({ ...current, [platform]: 'idle' }));
  };

  const handleContinue = async () => {
    // Workbench-only setups are valid, so allow an empty IM selection — do not
    // force a fallback platform into the enabled set. No user-facing primary:
    // the backend derives its internal default from ``enabled``.
    const normalized = selected;
    const selectionData = {
      platforms: {
        enabled: normalized,
      },
    };
    const nextData = {
      ...credentialDraft,
      discord_client_id: credentialDraft.discord?.client_id || '',
      ...selectionData,
    };

    if (isPage && onSave) {
      await onSave(nextData);
      return;
    }

    onNext(nextData);
  };

  const activeDescriptor = platformCatalog.find((item) => item.id === activeCredentialPlatform) || platformCatalog[0];
  const activePlatformLabel = t(activeDescriptor?.title_key || `platform.${activeCredentialPlatform}.title`);
  const activeCredential = credentialDraft[activeCredentialPlatform] || {};
  const currentValidationState = validationState[activeCredentialPlatform] || 'idle';

  const getExternalUrl = (platform: string) => {
    if (platform === 'slack') return 'https://api.slack.com/apps';
    if (platform === 'discord') return 'https://discord.com/developers/applications';
    if (platform === 'telegram') return 'https://t.me/BotFather';
    if (platform === 'lark') return activeCredential.domain === 'lark' ? 'https://open.larksuite.com/app' : 'https://open.feishu.cn/app';
    return '';
  };

  const canValidate = () => {
    if (activeCredentialPlatform === 'slack') return hasUsableSecret(activeCredential, 'bot_token');
    if (activeCredentialPlatform === 'discord') return hasUsableSecret(activeCredential, 'bot_token');
    if (activeCredentialPlatform === 'telegram') return hasUsableSecret(activeCredential, 'bot_token');
    if (activeCredentialPlatform === 'lark') return Boolean(activeCredential.app_id && hasUsableSecret(activeCredential, 'app_secret'));
    return false;
  };

  const validateActivePlatform = async () => {
    if (!canValidate()) return;

    setValidating(true);
    try {
      let result: any = { ok: false };
      if (activeCredentialPlatform === 'slack') {
        result = await api.slackAuthTest(activeCredential.bot_token);
      } else if (activeCredentialPlatform === 'discord') {
        result = await api.discordAuthTest(activeCredential.bot_token);
      } else if (activeCredentialPlatform === 'telegram') {
        result = await api.telegramAuthTest(activeCredential.bot_token);
      } else if (activeCredentialPlatform === 'lark') {
        result = await api.larkAuthTest(activeCredential.app_id, activeCredential.app_secret, activeCredential.domain);
      }

      const ok = result?.ok !== false;
      setValidationState((current) => ({ ...current, [activeCredentialPlatform]: ok ? 'success' : 'error' }));
      showToast(ok ? t('platform.validationSuccess') : t('platform.validationFailed'), ok ? 'success' : 'error');
    } catch {
      setValidationState((current) => ({ ...current, [activeCredentialPlatform]: 'error' }));
    } finally {
      setValidating(false);
    }
  };

  const renderCredentialFields = () => {
    if (activeCredentialPlatform === 'slack') {
      return (
        <>
          <CredentialInput
            label={t('slackConfig.botToken')}
            value={activeCredential.bot_token || ''}
            placeholder={t('slackConfig.botTokenPlaceholder')}
            hint={`${t('slackConfig.botTokenHint')} xoxb-`}
            onChange={(value) => updateCredential('slack', { bot_token: value })}
          />
          <CredentialInput
            label={t('slackConfig.appToken')}
            value={activeCredential.app_token || ''}
            placeholder={t('slackConfig.appTokenPlaceholder')}
            hint={`${t('slackConfig.appTokenHint')} xapp-`}
            onChange={(value) => updateCredential('slack', { app_token: value })}
          />
        </>
      );
    }

    if (activeCredentialPlatform === 'discord') {
      return (
        <>
          <CredentialInput
            label={t('discordConfig.clientId')}
            value={activeCredential.client_id || ''}
            placeholder={t('discordConfig.clientIdPlaceholder')}
            hint={t('discordConfig.clientIdHint')}
            type="text"
            onChange={(value) => updateCredential('discord', { client_id: value })}
          />
          <CredentialInput
            label={t('discordConfig.botToken')}
            value={activeCredential.bot_token || ''}
            placeholder={t('discordConfig.botTokenPlaceholder')}
            hint={t('discordConfig.botTokenHint')}
            onChange={(value) => updateCredential('discord', { bot_token: value })}
          />
        </>
      );
    }

    if (activeCredentialPlatform === 'telegram') {
      return (
        <>
          <CredentialInput
            label={t('telegramConfig.botToken')}
            value={activeCredential.bot_token || ''}
            placeholder={t('telegramConfig.botTokenPlaceholder')}
            hint={t('telegramConfig.botTokenHint')}
            onChange={(value) => updateCredential('telegram', { bot_token: value })}
          />
          <div className="grid gap-2 md:grid-cols-2">
            <CredentialToggle
              label={t('telegramConfig.requireMention')}
              checked={activeCredential.require_mention ?? true}
              onChange={(checked) => updateCredential('telegram', { require_mention: checked })}
            />
            <CredentialToggle
              label={t('telegramConfig.forumAutoTopic')}
              checked={activeCredential.forum_auto_topic ?? true}
              onChange={(checked) => updateCredential('telegram', { forum_auto_topic: checked })}
            />
          </div>
        </>
      );
    }

    if (activeCredentialPlatform === 'lark') {
      return (
        <>
          <label className="grid gap-1.5">
            <span className="text-[11px] font-medium text-muted">{t('larkConfig.domainLabel')}</span>
            <Select
              value={activeCredential.domain || 'feishu'}
              onChange={(event) => updateCredential('lark', { domain: event.target.value })}
              className="text-[12px]"
            >
              <option value="feishu">{t('larkConfig.domainFeishu')}</option>
              <option value="lark">{t('larkConfig.domainLark')}</option>
            </Select>
          </label>
          <CredentialInput
            label={t('larkConfig.appId')}
            value={activeCredential.app_id || ''}
            placeholder={t('larkConfig.appIdPlaceholder')}
            hint={t('larkConfig.appIdHint')}
            type="text"
            onChange={(value) => updateCredential('lark', { app_id: value })}
          />
          <CredentialInput
            label={t('larkConfig.appSecret')}
            value={activeCredential.app_secret || ''}
            placeholder={t('larkConfig.appSecretPlaceholder')}
            hint={t('larkConfig.appSecretHint')}
            onChange={(value) => updateCredential('lark', { app_secret: value })}
          />
        </>
      );
    }

    return (
      <div className="rounded-md border border-border bg-background px-3 py-3 text-[12px] leading-relaxed text-muted">
        {t('platform.wechatCredentialHint')}
      </div>
    );
  };

  if (isPage) {
    return (
      <div className="mx-auto max-w-[920px]">
        <section className="overflow-hidden rounded-md border border-border bg-surface-2/45">
          <div className="border-b border-border px-4 py-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h2 className="text-[12px] font-semibold text-foreground">{t('platform.credentialsTitle')}</h2>
                <p className="mt-1 max-w-2xl text-[10px] leading-relaxed text-muted">{t('platform.credentialsSubtitle')}</p>
              </div>
              <button
                type="button"
                className="inline-flex h-6 shrink-0 items-center rounded-md border border-border bg-surface-3 px-2 text-[10px] font-medium text-foreground"
              >
                {t('common.edit')}
              </button>
            </div>

            <div className="mt-3 flex flex-wrap gap-1.5">
              {platformCatalog.map((platform) => {
                const option = platform.id;
                const active = selected.includes(option);
                const focused = activeCredentialPlatform === option;
                const label = t(platform.title_key || `platform.${option}.title`);
                return (
                  <button
                    key={option}
                    type="button"
                    onClick={() => {
                      if (!active) {
                        togglePlatform(option);
                      }
                      setActiveCredentialPlatform(option);
                    }}
                    className={clsx(
                      'inline-flex h-7 min-w-[118px] items-center justify-between gap-2 rounded-[5px] border px-2 text-[11px] font-medium transition-colors',
                      active
                        ? 'border-mint/35 bg-mint-soft text-foreground'
                        : 'border-border bg-surface-3 text-muted hover:border-border-strong',
                      focused && 'ring-1 ring-mint/30'
                    )}
                  >
                    <span className="truncate">{label}</span>
                    {active ? <Check className="size-3 text-mint" /> : <Plus className="size-3" />}
                  </button>
                );
              })}
            </div>

            <div className="mt-2 flex flex-col gap-2 border-t border-border pt-2 md:flex-row md:items-center md:justify-between">
              <p className="text-[10px] text-muted">{t('platform.applyHint')}</p>
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  className="h-6 rounded-md border border-border bg-surface-3 px-2 text-[10px] font-medium text-foreground"
                >
                  {t('common.cancel')}
                </button>
                <button
                  type="button"
                  onClick={() => void handleContinue()}
                  disabled={!selected.length}
                  className="inline-flex h-6 items-center gap-1.5 rounded-md bg-primary px-2.5 text-[10px] font-semibold text-primary-foreground disabled:opacity-50"
                >
                  <Check className="size-3" />
                  {t('platform.apply')}
                </button>
              </div>
            </div>
          </div>

          <div className="border-b border-border px-4 py-2">
            <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
              <div className="min-w-0">
                <h2 className="text-[12px] font-semibold text-foreground">
                  {t('platform.credentialSetupTitle', { platform: activePlatformLabel })}
                </h2>
                <p className="mt-0.5 text-[10px] text-muted">{t('platform.credentialSetupSubtitle')}</p>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {selected.map((platform) => {
                  const descriptor = platformCatalog.find((item) => item.id === platform);
                  return (
                    <button
                      key={platform}
                      type="button"
                      onClick={() => setActiveCredentialPlatform(platform)}
                      className={clsx(
                        'h-6 rounded-md border px-2.5 text-[10px] font-medium transition-colors',
                        activeCredentialPlatform === platform
                          ? 'border-mint/40 bg-mint-soft text-mint'
                          : 'border-border bg-surface-3 text-muted hover:border-border-strong'
                      )}
                    >
                      {t(descriptor?.title_key || `platform.${platform}.title`)}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>

          <div>
            {[
              t('platform.stepCreateApp'),
              activeCredentialPlatform === 'slack' ? t('slackConfig.step2Title') : t('platform.stepAddBotToken'),
              activeCredentialPlatform === 'slack' ? t('slackConfig.step3Title') : t('platform.stepConfigureOptions'),
              activeCredentialPlatform === 'slack' ? t('slackConfig.step4Title') : t('platform.stepValidate'),
            ].map((title, index) => {
              const expanded = index === 2;
              return (
                <div
                  key={`${activeCredentialPlatform}-${title}`}
                  className={clsx('border-b border-border px-4 py-2 last:border-b-0', expanded && 'bg-mint-soft/10')}
                >
                  <div className="flex items-center justify-between gap-3">
                    <div className="flex min-w-0 items-center gap-3">
                      <span
                        className={clsx(
                          'flex size-4 shrink-0 items-center justify-center rounded-full border text-[9px] font-semibold',
                          index < 2 || currentValidationState === 'success'
                            ? 'border-mint bg-mint text-background'
                            : 'border-border bg-background text-muted'
                        )}
                      >
                        {index < 2 || currentValidationState === 'success' ? <Check className="size-2.5" /> : index + 1}
                      </span>
                      <div className="min-w-0">
                        <div className="text-[11px] font-medium text-foreground">{title}</div>
                        {expanded && (
                          <div className="mt-0.5 text-[9px] text-muted">
                            {t('platform.expandedStepHint', { platform: activePlatformLabel })}
                          </div>
                        )}
                      </div>
                    </div>
                    <div className="text-[9px] text-muted">{expanded ? t('platform.expanded') : t('platform.collapsed')}</div>
                  </div>

                  {expanded && (
                    <div className="mt-2 rounded-md border border-border bg-background/70 p-2.5">
                      <div className="rounded-md border border-border bg-surface-3 px-2.5 py-2 text-[10px] leading-relaxed text-muted">
                        {t('platform.credentialGuide', { platform: activePlatformLabel })}
                      </div>

                      <div className="mt-2 grid gap-2">{renderCredentialFields()}</div>

                      <div className="mt-2 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
                        {getExternalUrl(activeCredentialPlatform) ? (
                          <button
                            type="button"
                            onClick={() => window.open(getExternalUrl(activeCredentialPlatform), '_blank')}
                            className="inline-flex h-7 items-center justify-center gap-1.5 rounded-md border border-border bg-surface-3 px-2.5 text-[10px] font-medium text-foreground hover:border-border-strong"
                          >
                            <ExternalLink className="size-3" />
                            {t('platform.openConsole', { platform: activePlatformLabel })}
                          </button>
                        ) : <span />}
                        <button
                          type="button"
                          onClick={() => void validateActivePlatform()}
                          disabled={!canValidate() || validating || activeCredentialPlatform === 'wechat'}
                          className="inline-flex h-7 items-center justify-center gap-1.5 rounded-md bg-primary px-2.5 text-[10px] font-semibold text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {validating ? <Loader2 className="size-3 animate-spin" /> : <Check className="size-3" />}
                          {t('platform.validateToken')}
                        </button>
                      </div>

                      {currentValidationState !== 'idle' && (
                        <div
                          className={clsx(
                            'mt-2 rounded-md border px-2.5 py-1.5 text-[10px]',
                            currentValidationState === 'success'
                              ? 'border-mint/30 bg-mint-soft text-mint'
                              : 'border-danger/30 bg-danger/10 text-danger'
                          )}
                        >
                          {currentValidationState === 'success' ? t('platform.validationSuccess') : t('platform.validationFailed')}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      </div>
    );
  }

  // Wizard mode — design.pen jXiOv: WizardCard with a required always-on Avibe
  // Workbench card on the left and an optional grid of third-party chat
  // platforms on the right. Primary platform is implicit: first selected, then
  // locked to that order.
  const chatPlatforms = getImPlatforms(data);
  return (
    <div className="flex w-full justify-center">
      <WizardCard className="gap-8">
        <div className="flex flex-col gap-3">
          <EyebrowBadge tone="mint">{t('platform.eyebrow')}</EyebrowBadge>
          <h2 className="text-[26px] font-bold leading-[1.15] tracking-[-0.6px] text-foreground sm:text-[34px] sm:leading-[1.1]">
            {t('platform.title')}
          </h2>
          <p className="max-w-[680px] text-[15px] leading-[1.5] text-muted">{t('platform.subtitle')}</p>
        </div>

        <div className="flex flex-col items-stretch gap-5 lg:flex-row">
          {/* Required always-on workbench — not toggleable. */}
          <div className="flex w-full flex-col gap-3.5 rounded-xl border-2 border-mint bg-mint/[0.08] p-5 lg:w-[340px]">
            <div className="flex items-center justify-between">
              <span className="inline-flex size-[52px] items-center justify-center rounded-xl border border-mint/40 bg-mint/10">
                <PlatformIcon platform={WORKBENCH_PLATFORM_ID} size={28} />
              </span>
              <span className="inline-flex items-center gap-1 rounded-full bg-mint px-2.5 py-1 text-[10px] font-bold text-primary-foreground">
                <Lock className="size-3" />
                {t('platform.workbenchRequired')}
              </span>
            </div>
            <div className="flex flex-col gap-1.5">
              <h3 className="text-[17px] font-bold text-foreground">{t('platform.avibe.title')}</h3>
              <p className="text-[13px] leading-[1.5] text-muted">{t('platform.workbenchDesc')}</p>
            </div>
            <div className="flex items-center gap-2 rounded-lg border border-cyan/30 bg-cyan/[0.08] px-3 py-2.5">
              <Smartphone className="size-4 shrink-0 text-cyan" />
              <span className="text-[12px] leading-[1.4] text-foreground">{t('platform.workbenchPwa')}</span>
            </div>
            <div className="mt-auto flex items-center gap-2">
              <CircleCheckBig className="size-4 shrink-0 text-mint" />
              <span className="text-[12px] font-semibold text-mint">{t('platform.workbenchAlwaysOn')}</span>
            </div>
          </div>

          {/* Optional third-party chat platforms. */}
          <div className="flex flex-1 flex-col gap-3">
            <div className="flex items-center justify-between">
              <h3 className="text-[14px] font-semibold text-foreground">{t('platform.chatPlatformsTitle')}</h3>
              <span className="rounded-full border border-border px-2.5 py-0.5 text-[10px] text-muted">
                {t('platform.optionalLabel')}
              </span>
            </div>
            <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-3 sm:gap-3">
              {chatPlatforms.map((platform) => {
                const option = platform.id;
                const active = selected.includes(option);
                const tileTint = PLATFORM_TILE_STYLES[option] || {
                  bg: 'bg-foreground/[0.04]',
                  border: 'border-foreground/[0.10]',
                };
                return (
                  <button
                    type="button"
                    key={option}
                    onClick={() => togglePlatform(option)}
                    className={clsx(
                      // Phone: a full-width row (icon · name · select indicator).
                      // sm+: the design's centered vertical tile in a 3-col grid.
                      'flex flex-row items-center gap-3 rounded-xl px-4 py-3 text-left transition-colors sm:flex-col sm:gap-2.5 sm:py-5 sm:text-center',
                      active
                        ? 'border-2 border-mint bg-mint/[0.16]'
                        : 'border border-foreground/[0.08] bg-background hover:border-foreground/[0.16] hover:bg-foreground/[0.02]'
                    )}
                  >
                    <span
                      className={clsx(
                        'inline-flex size-11 shrink-0 items-center justify-center rounded-[10px] border',
                        tileTint.bg,
                        tileTint.border
                      )}
                    >
                      <PlatformIcon platform={option} size={22} />
                    </span>
                    <span
                      className={clsx(
                        'text-[14px] leading-tight transition-colors sm:text-[13px]',
                        active ? 'font-bold text-foreground' : 'font-medium text-muted'
                      )}
                    >
                      {t(platform.title_key || `platform.${option}.title`)}
                    </span>
                    {/* Selection indicator on the phone row only; on sm+ the
                        mint border carries the selected state (design.pen). */}
                    {active ? (
                      <CircleCheckBig className="ml-auto size-5 shrink-0 text-mint sm:hidden" strokeWidth={2.25} />
                    ) : (
                      <Circle className="ml-auto size-5 shrink-0 text-muted/40 sm:hidden" strokeWidth={2} />
                    )}
                  </button>
                );
              })}
            </div>
          </div>
        </div>

        <div className="flex items-center justify-between gap-3 border-t border-border pt-4">
          <Button
            type="button"
            variant="secondary"
            size="default"
            onClick={onBack}
            className="font-semibold"
          >
            <ArrowLeft size={14} strokeWidth={2.25} />
            {t('common.back')}
          </Button>
          <Button
            type="button"
            variant="brand"
            size="default"
            onClick={() => void handleContinue()}
            className="flex-1 sm:flex-none"
          >
            {t('common.continue')}
            <ArrowRight size={14} strokeWidth={2.25} />
          </Button>
        </div>
      </WizardCard>
    </div>
  );
};

const CredentialInput: React.FC<{
  label: string;
  value: string;
  placeholder?: string;
  hint?: string;
  type?: 'password' | 'text';
  onChange: (value: string) => void;
}> = ({ label, value, placeholder, hint, type = 'password', onChange }) => (
  <label className="grid gap-1.5">
    <span className="text-[11px] font-medium text-muted">{label}</span>
    <Input
      type={type}
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
      className="text-[12px]"
    />
    {hint ? <span className="text-[10px] text-muted">{hint}</span> : null}
  </label>
);

const CredentialToggle: React.FC<{
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}> = ({ label, checked, onChange }) => (
  <button
    type="button"
    onClick={() => onChange(!checked)}
    className="flex h-10 items-center justify-between gap-3 rounded-md border border-border bg-background px-3 text-left"
  >
    <span className="text-[11px] font-medium text-foreground">{label}</span>
    <span className={clsx('relative h-5 w-9 rounded-full transition-colors', checked ? 'bg-mint' : 'bg-surface-3')}>
      <span
        className={clsx(
          'absolute top-0.5 size-4 rounded-full bg-background transition-transform',
          checked ? 'translate-x-4' : 'translate-x-0.5'
        )}
      />
    </span>
  </button>
);
