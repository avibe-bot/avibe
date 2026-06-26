import React, { useEffect, useMemo, useState } from 'react';
import { ArrowRight, Bot, HelpCircle, MessageSquare, Radio, Send, Sparkles, Type } from 'lucide-react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { useApi } from '@/context/ApiContext';
import { SettingsPageShell } from './SettingsPageShell';
import {
  platformHasCapability,
  getEnabledPlatforms,
} from '@/lib/platforms';
import {
  CompactField,
  CompactSelect,
  SettingsPanel,
  SettingsRow,
  ToggleSwitch,
} from './SettingsPrimitives';
import {
  DEFAULT_CHAT_MESSAGE_FONT_SIZE,
  MAX_CHAT_MESSAGE_FONT_SIZE,
  MIN_CHAT_MESSAGE_FONT_SIZE,
  normalizeChatMessageFontSize,
} from '@/lib/chatDisplay';

const SAVE_KEYS = [
  'platforms',
  'ack_mode',
  'show_duration',
  'include_time_info',
  'include_user_info',
  'reply_enhancements',
  'show_pages_prompt',
  'agent_progress_style',
  'audio_asr',
  'slack',
  'discord',
  'telegram',
  'lark',
  'wechat',
  'agents',
] as const;

function buildMessagePatch(config: any, extraPatch: Record<string, unknown> = {}) {
  // ``save_config`` merges onto the stored config and the backend derives the
  // internal default platform from ``platforms.enabled``, so this messaging
  // save no longer sends a ``platform``/primary field.
  const patch: Record<string, unknown> = {};

  for (const key of SAVE_KEYS) {
    patch[key] = config?.[key];
  }

  return { ...patch, ...extraPatch };
}

function formatSavedAt(value: number | null, t: (key: string) => string) {
  if (!value) return t('settings.messagingStatusIdle');
  const deltaSec = Math.max(0, Math.round((Date.now() - value) / 1000));
  return deltaSec <= 1
    ? t('settings.messagingStatusJustNow')
    : t('settings.messagingStatusAgo').replace('{{seconds}}', String(deltaSec));
}

// Mirrors design.pen TDgw0 (VR/CM/Messaging):
// msgIntro 15px semibold + 12px muted, msgSec1 cornerRadius 12 fill --background
// stroke --border, value rows padding [14, 20] separated by bottom border.
export const SettingsMessagingPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const [config, setConfig] = useState<any>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [chatFontDraft, setChatFontDraft] = useState('');

  useEffect(() => {
    api.getConfig().then(setConfig).catch(() => {});
  }, [api]);

  useEffect(() => {
    if (!config) return;
    setChatFontDraft(String(normalizeChatMessageFontSize(config.ui?.chat_message_font_size)));
  }, [config?.ui?.chat_message_font_size]);

  const enabledPlatforms = useMemo(() => getEnabledPlatforms(config), [config]);
  const slackSupportsLinkUnfurl = enabledPlatforms.includes('slack');
  const reactionSupported = enabledPlatforms.some((platform) =>
    platformHasCapability(config, platform, 'supports_reaction_indicator')
  );
  const typingSupported = enabledPlatforms.some((platform) =>
    platformHasCapability(config, platform, 'supports_typing_indicator')
  );

  const persist = async (nextConfig: any, extraPatch?: Record<string, unknown>) => {
    setConfig(nextConfig);
    setSaveError(null);
    try {
      await api.saveConfig(buildMessagePatch(nextConfig, extraPatch));
      setSavedAt(Date.now());
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : t('common.saveFailed'));
    }
  };

  if (!config) {
    return (
      <SettingsPageShell
        activeTab="messaging"
        title={t('settings.messagingTitle')}
        subtitle={t('settings.messagingSubtitle')}
      >
        <div className="text-[13px] text-muted">{t('common.loading')}</div>
      </SettingsPageShell>
    );
  }

  const ackOptions = [
    { value: 'typing', label: t('dashboard.ackTyping'), disabled: !typingSupported },
    { value: 'reaction', label: t('dashboard.ackReaction'), disabled: !reactionSupported },
    { value: 'message', label: t('dashboard.ackMessage'), disabled: false },
  ];
  const progressStyleOptions = [
    { value: 'concise', label: t('dashboard.agentProgressStyleConcise') },
    { value: 'verbose', label: t('dashboard.agentProgressStyleVerbose') },
    { value: 'off', label: t('dashboard.agentProgressStyleOff') },
  ];
  const includeTimeInfoEnabled = config.include_time_info !== false;
  const audioAsr = config.audio_asr || {};
  const audioEchoEnabled = audioAsr.echo_transcript !== false;
  const vibeCloud = config.remote_access?.vibe_cloud || {};
  const vibeCloudPaired = Boolean(vibeCloud.enabled && vibeCloud.instance_id);
  const audioAsrEnabled = vibeCloudPaired && audioAsr.enabled !== false;
  const chatMessageFontSize = normalizeChatMessageFontSize(config.ui?.chat_message_font_size);
  const saveChatMessageFontSize = (fontSize: number) => {
    const nextConfig = {
      ...config,
      ui: {
        ...(config.ui || {}),
        chat_message_font_size: fontSize,
      },
    };
    void persist(nextConfig, { ui: { chat_message_font_size: fontSize } });
  };
  const updateChatFontDraft = (rawValue: string) => {
    setChatFontDraft(rawValue);

    const numericValue = Number(rawValue);
    if (
      !rawValue.trim() ||
      !Number.isFinite(numericValue) ||
      numericValue < MIN_CHAT_MESSAGE_FONT_SIZE ||
      numericValue > MAX_CHAT_MESSAGE_FONT_SIZE
    ) {
      return;
    }

    const nextFontSize = normalizeChatMessageFontSize(numericValue);
    if (nextFontSize !== chatMessageFontSize) {
      saveChatMessageFontSize(nextFontSize);
    }
  };
  const commitChatFontDraft = () => {
    const rawValue = chatFontDraft.trim();
    if (!rawValue) {
      setChatFontDraft(String(DEFAULT_CHAT_MESSAGE_FONT_SIZE));
      if (chatMessageFontSize !== DEFAULT_CHAT_MESSAGE_FONT_SIZE) {
        saveChatMessageFontSize(DEFAULT_CHAT_MESSAGE_FONT_SIZE);
      }
      return;
    }

    const nextFontSize = normalizeChatMessageFontSize(rawValue);
    setChatFontDraft(String(nextFontSize));
    if (nextFontSize !== chatMessageFontSize) {
      saveChatMessageFontSize(nextFontSize);
    }
  };

  return (
    <SettingsPageShell
      activeTab="messaging"
      title={t('settings.messagingTitle')}
      subtitle={t('settings.messagingSubtitle')}
      actions={
        <div className="flex items-center gap-2">
          <span
            className={clsx(
              'inline-flex items-center rounded-full border px-2.5 py-0.5 text-[11px] font-semibold uppercase tracking-[0.14em]',
              saveError
                ? 'border-danger/30 bg-danger/10 text-danger'
                : 'border-mint/30 bg-mint/[0.08] text-mint'
            )}
          >
            {saveError ? t('common.saveFailed') : t('settings.messagingAutosaved')}
          </span>
          <span className="font-mono text-[10px] text-muted">
            {saveError || formatSavedAt(savedAt, t)}
          </span>
        </div>
      }
    >
      <SettingsPanel
        title={
          <span className="inline-flex items-center gap-2">
            <Sparkles className="size-3.5 text-cyan" />
            {t('settings.messagingInputEnrichmentTitle')}
          </span>
        }
        description={t('settings.messagingInputEnrichmentDescription')}
      >
        <SettingsRow
          title={
            <span className="inline-flex items-center gap-1.5">
              {t('dashboard.audioTranscription')}
              {!vibeCloudPaired && (
                <span className="group relative inline-flex">
                  <HelpCircle className="size-3.5 cursor-help text-muted/60" />
                  <span className="pointer-events-none absolute bottom-full left-0 z-20 mb-2 w-64 whitespace-normal rounded bg-text px-3 py-2 text-[11px] font-normal text-bg opacity-0 shadow-lg transition-opacity group-hover:opacity-100">
                    {t('dashboard.audioTranscriptionRequiresRemoteAccessTooltip')}
                  </span>
                </span>
              )}
            </span>
          }
          description={
            vibeCloudPaired
              ? t('dashboard.audioTranscriptionHint')
              : t('dashboard.audioTranscriptionRequiresVibeCloud')
          }
          control={
            <ToggleSwitch
              enabled={audioAsrEnabled}
              disabled={!vibeCloudPaired}
              onClick={() =>
                void persist({
                  ...config,
                  audio_asr: {
                    ...audioAsr,
                    enabled: !audioAsrEnabled,
                    enabled_configured: true,
                  },
                })
              }
            />
          }
        />

        <SettingsRow
          title={t('dashboard.audioTranscriptEcho')}
          description={t('dashboard.audioTranscriptEchoHint')}
          control={
            <ToggleSwitch
              enabled={audioEchoEnabled}
              disabled={!audioAsrEnabled}
              onClick={() =>
                void persist({
                  ...config,
                  audio_asr: {
                    ...audioAsr,
                    echo_transcript: !audioEchoEnabled,
                  },
                })
              }
            />
          }
        />
      </SettingsPanel>

      <SettingsPanel
        title={
          <span className="inline-flex items-center gap-2">
            <Bot className="size-3.5 text-mint" />
            {t('settings.messagingAgentContextTitle')}
          </span>
        }
        description={t('settings.messagingAgentContextDescription')}
      >
        <SettingsRow
          title={t('dashboard.includeTimeInfo')}
          description={t('dashboard.includeTimeInfoHint')}
          control={
            <ToggleSwitch
              enabled={includeTimeInfoEnabled}
              onClick={() =>
                void persist({ ...config, include_time_info: !includeTimeInfoEnabled })
              }
            />
          }
        />

        <SettingsRow
          title={t('dashboard.includeUserInfo')}
          description={t('dashboard.includeUserInfoHint')}
          control={
            <ToggleSwitch
              enabled={Boolean(config.include_user_info)}
              onClick={() =>
                void persist({ ...config, include_user_info: !config.include_user_info })
              }
            />
          }
        />
      </SettingsPanel>

      <SettingsPanel
        title={
          <span className="inline-flex items-center gap-2">
            <Radio className="size-3.5 text-gold" />
            {t('settings.messagingWorkFeedbackTitle')}
          </span>
        }
        description={t('settings.messagingWorkFeedbackDescription')}
      >
        <SettingsRow
          title={t('dashboard.ackMode')}
          description={t('dashboard.ackModeHint')}
          control={
            <CompactSelect
              value={config.ack_mode || 'typing'}
              onChange={(event) =>
                void persist({ ...config, ack_mode: event.target.value || 'typing' })
              }
              className="w-40"
            >
              {ackOptions.map((option) => (
                <option key={option.value} value={option.value} disabled={option.disabled}>
                  {option.label}
                </option>
              ))}
            </CompactSelect>
          }
        />

        <SettingsRow
          title={t('dashboard.agentProgressStyle')}
          description={t('dashboard.agentProgressStyleHint')}
          control={
            <CompactSelect
              value={config.agent_progress_style || 'concise'}
              onChange={(event) =>
                void persist({
                  ...config,
                  agent_progress_style: event.target.value || 'concise',
                })
              }
              className="w-40"
            >
              {progressStyleOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </CompactSelect>
          }
        />

        <SettingsRow
          title={t('dashboard.errorRetryLimit')}
          description={t('dashboard.errorRetryLimitHint')}
          control={
            <CompactField
              type="number"
              min={0}
              max={10}
              value={config.agents?.opencode?.error_retry_limit ?? 1}
              onChange={(event) => {
                const limit = Math.max(0, Math.min(10, Number(event.target.value) || 0));
                void persist({
                  ...config,
                  agents: {
                    ...(config.agents || {}),
                    opencode: {
                      ...(config.agents?.opencode || {}),
                      error_retry_limit: limit,
                    },
                  },
                });
              }}
              className="w-24 text-center font-mono"
            />
          }
        />

        <SettingsRow
          title={t('dashboard.showDuration')}
          description={t('dashboard.showDurationHint')}
          control={
            <ToggleSwitch
              enabled={config.show_duration !== false}
              onClick={() => void persist({ ...config, show_duration: !config.show_duration })}
            />
          }
        />
      </SettingsPanel>

      <SettingsPanel
        title={
          <span className="inline-flex items-center gap-2">
            <Send className="size-3.5 text-cyan" />
            {t('settings.messagingReplyExperienceTitle')}
          </span>
        }
        description={t('settings.messagingReplyExperienceDescription')}
      >
        <SettingsRow
          title={t('dashboard.replyEnhancements')}
          description={t('dashboard.replyEnhancementsHint')}
          control={
            <ToggleSwitch
              enabled={config.reply_enhancements !== false}
              onClick={() =>
                void persist({ ...config, reply_enhancements: !config.reply_enhancements })
              }
            />
          }
        />
        <SettingsRow
          title={t('dashboard.showPagesPrompt')}
          description={t('dashboard.showPagesPromptHint')}
          control={
            <ToggleSwitch
              enabled={config.show_pages_prompt !== false}
              onClick={() =>
                void persist({
                  ...config,
                  show_pages_prompt: !(config.show_pages_prompt !== false),
                })
              }
            />
          }
        />
        {slackSupportsLinkUnfurl && (
          <SettingsRow
            title={t('dashboard.slackLinkPreviews')}
            description={t('dashboard.slackLinkPreviewsHint')}
            control={
              <ToggleSwitch
                enabled={Boolean(config.slack?.disable_link_unfurl)}
                onClick={() =>
                  void persist({
                    ...config,
                    slack: {
                      ...(config.slack || {}),
                      disable_link_unfurl: !config.slack?.disable_link_unfurl,
                    },
                  })
                }
              />
            }
          />
        )}
      </SettingsPanel>

      <SettingsPanel
        title={
          <span className="inline-flex items-center gap-2">
            <Type className="size-3.5 text-mint" />
            {t('settings.messagingDisplayTitle')}
          </span>
        }
        description={t('settings.messagingDisplayDescription')}
      >
        <SettingsRow
          title={t('dashboard.chatMessageFontSize')}
          description={t('dashboard.chatMessageFontSizeHint')}
          control={
            <div className="flex items-center gap-2">
              <CompactField
                type="number"
                min={MIN_CHAT_MESSAGE_FONT_SIZE}
                max={MAX_CHAT_MESSAGE_FONT_SIZE}
                step={1}
                inputMode="numeric"
                value={chatFontDraft}
                onChange={(event) => updateChatFontDraft(event.target.value)}
                onBlur={commitChatFontDraft}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.currentTarget.blur();
                  }
                }}
                className="w-20 text-center font-mono"
              />
              <span className="font-mono text-[11px] text-muted">px</span>
            </div>
          }
        />
      </SettingsPanel>

      <SettingsPanel
        title={
          <span className="inline-flex items-center gap-2">
            <MessageSquare className="size-3.5 text-mint" />
            {t('settings.messagingGroupsTitle')}
          </span>
        }
        description={t('settings.messagingGroupsDescription')}
      >
        <SettingsRow
          title={t('dashboard.allowedChannels')}
          description={t('settings.messagingGroupsHint')}
          control={
            <Link
              to="/admin/groups"
              className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border bg-foreground/[0.04] px-3 text-[12px] font-medium text-foreground transition hover:border-border-strong"
            >
              {t('common.manageChannels')}
              <ArrowRight className="size-3.5" strokeWidth={2.25} />
            </Link>
          }
        />
      </SettingsPanel>
    </SettingsPageShell>
  );
};
