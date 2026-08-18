import React, { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useTranslation } from 'react-i18next';
import { Welcome } from './steps/Welcome';
import { PlatformSelection } from './steps/PlatformSelection';
import { AgentDetection } from './steps/AgentDetection';
import { SlackConfig } from './steps/SlackConfig';
import { DiscordConfig } from './steps/DiscordConfig';
import { TelegramConfig } from './steps/TelegramConfig';
import { LarkConfig } from './steps/LarkConfig';
import { WeChatConfig } from './steps/WeChatConfig';
import { ChannelList } from './steps/ChannelList';
import { Summary } from './steps/Summary';
import { useApi } from '../context/ApiContext';
import clsx from 'clsx';
import {
  getEnabledPlatforms,
  platformHasRunnableConfig,
  platformSupportsChannels,
} from '../lib/platforms';
import { withoutConfiguredSecretMarker, withSecretDraft, withSecretDrafts } from '../lib/secretFields';
import { WizardChrome } from './visual';

const getPersistableWizardPlatforms = (data: any) =>
  getEnabledPlatforms(data).filter((platform) => platformHasRunnableConfig(data, platform));

const buildConfigPayload = (data: any, enabledPlatformOverride?: string[]) => {
  const enabledPlatforms = enabledPlatformOverride ?? getEnabledPlatforms(data);
  // Deselection encoding: baseline = platforms enabled when the wizard
  // loaded its config. A baseline platform the wizard no longer selects
  // is an explicit user deselection → remove operation. Platforms added
  // by other processes after load are not in the baseline and are
  // preserved by construction.
  const baseline: string[] = Array.isArray(data.__wizardEnabledBaseline)
    ? data.__wizardEnabledBaseline
    : [];
  const deselected = baseline.filter((p) => !enabledPlatforms.includes(p));

  return {
  // Patch-write shape (#1458 stage ③): the wizard sends only what the
  // wizard flow owns — the platform sections the user configured, the
  // wizard-owned agent toggles, mode/version, and its default cwd.
  // Enablement is expressed as add/remove operations against the
  // persisted list (list-ops verb) instead of a whole enabled-array
  // snapshot, so a concurrently enabled platform (e.g. WeChat QR
  // confirmation while the wizard sits open) is never dropped, while
  // explicit wizard deselections still take effect. Sections the wizard
  // does not edit (runtime extras, ui, update, gateway, language,
  // ack_mode) are left to their owners instead of round-tripped.
  __avibe_list_ops: {
    'platforms.enabled': { add: enabledPlatforms, remove: deselected },
  },
  mode: data.mode || 'self_host',
  version: 'v2',
  // The wizard explicitly forces duration output off when WeChat is in
  // the selection (WeChat cannot render it) — a wizard-authored override
  // that must persist with the wizard's own save.
  ...(data.show_duration !== undefined ? { show_duration: data.show_duration } : {}),
  slack: {
    // Preserve all existing slack fields
    ...withSecretDrafts(data.slack, {
      bot_token: data.slack?.bot_token,
      app_token: data.slack?.app_token,
    }),
    // Override only the fields that setup modifies
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
    base_url: data.wechat?.base_url || 'https://ilinkai.weixin.qq.com',
    cdn_base_url: data.wechat?.cdn_base_url || 'https://novac2c.cdn.weixin.qq.com/c2c',
    require_mention: data.wechat?.require_mention || false,
  },
  runtime: {
    // The wizard owns only the default cwd it captured.
    default_cwd: data.default_cwd || data.runtime?.default_cwd || '.',
  },
  agents: {
    // The wizard owns the enablement toggle and the CLI path chosen in
    // its Agents step; spreading the loaded section would round-trip
    // stale sibling fields (e.g. an installer-updated cli_path).
    opencode: {
      enabled: data.agents?.opencode?.enabled ?? true,
      cli_path: data.agents?.opencode?.cli_path || 'opencode',
      default_agent: data.opencode_default_agent ?? data.agents?.opencode?.default_agent ?? null,
      default_reasoning_effort: data.opencode_default_reasoning_effort ?? data.agents?.opencode?.default_reasoning_effort ?? null,
    },
    claude: {
      enabled: data.agents?.claude?.enabled ?? true,
      cli_path: data.agents?.claude?.cli_path || 'claude',
    },
    codex: {
      enabled: data.agents?.codex?.enabled ?? false,
      cli_path: data.agents?.codex?.cli_path || 'codex',
    },
  },
  };
};

export const Wizard: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const [currentStep, setCurrentStep] = useState(0);
  const [data, setData] = useState<any>({ show_duration: false });
  const [loaded, setLoaded] = useState(false);

  const steps = React.useMemo(() => {
    // ``platforms.enabled`` only ever contains real IM platforms — the
    // always-on workbench is stripped by PlatformsConfig.validate() before any
    // config reaches the UI — so these are the platforms that need credential +
    // channel steps. (The workbench has no credentials or channels to set up.)
    const enabledPlatforms = getEnabledPlatforms(data);
    const platformSteps = enabledPlatforms.map((platform) => {
      const component = platform === 'discord'
        ? DiscordConfig
        : platform === 'telegram'
          ? TelegramConfig
        : platform === 'lark'
          ? LarkConfig
          : platform === 'wechat'
            ? WeChatConfig
            : SlackConfig;
      return {
        id: `platform-${platform}`,
        title: platform,
        component,
      };
    });

    // Channel steps: merge into a single step with platform tabs (instead of one step per platform)
    const channelPlatforms = enabledPlatforms.filter((platform) => platformSupportsChannels(data, platform));
    const channelStep = channelPlatforms.length > 0
      ? [{ id: 'channels', title: t('nav.channels'), component: (props: any) => <ChannelList {...props} wizardPlatforms={channelPlatforms} /> }]
      : [];

    return [
      { id: 'welcome', title: 'Welcome', component: Welcome },
      // { id: 'mode', title: 'Mode', component: ModeSelection }, // temporarily hidden — SaaS mode not yet available
      { id: 'agents', title: 'Agents', component: AgentDetection },
      { id: 'platform', title: 'Platform', component: PlatformSelection },
      ...platformSteps,
      ...channelStep,
      { id: 'summary', title: 'Finish', component: Summary },
    ];
  }, [data, t]);

  useEffect(() => {
    const bootstrap = async () => {
      let platformCatalog: any[] = [];
      try {
        const catalog = await api.getPlatformCatalog();
        platformCatalog = catalog?.platforms || [];
      } catch {
        // Config payloads from newer backends also include the catalog.
      }

      try {
        const config = await api.getConfig();
        const configWithCatalog = {
          ...config,
          platform_catalog: config.platform_catalog || platformCatalog,
        };
        const enabledPlatforms = getEnabledPlatforms(configWithCatalog);
        const settingsEntries = await Promise.all(
          enabledPlatforms.map(async (platform) => [platform, await api.getSettings(platform)] as const)
        );
        const channelConfigsByPlatform = Object.fromEntries(
          settingsEntries.map(([platform, settings]) => [platform, settings.channels || {}])
        );
        const discordSettings = settingsEntries.find(([platform]) => platform === 'discord')?.[1];
          setData({
            ...configWithCatalog,
            discordGuildAllowlist: discordSettings?.guild_allowlist || [],
            channelConfigsByPlatform,
            // Baseline for deselection encoding: platforms enabled when
            // the wizard loaded. A user deselecting one of these must
            // produce a REMOVE operation on save — an add-only verb
            // would silently leave the adapter enabled and receiving
            // messages. Concurrent additions by other processes are NOT
            // in this baseline and are preserved.
            __wizardEnabledBaseline: enabledPlatforms,
            agents: {
              opencode: config.agents?.opencode,
              claude: config.agents?.claude,
              codex: config.agents?.codex,
            },
          });

      } catch {
        setData((current: any) => ({
          ...current,
          platform_catalog: platformCatalog,
        }));
      } finally {
        setLoaded(true);
      }
    };
    bootstrap();
  }, []);

  const next = async (stepData: any) => {
    const previousPlatforms = getEnabledPlatforms(data);
    const nextPlatforms = getEnabledPlatforms({ ...data, ...stepData });
    const platformsChanged = previousPlatforms.join(',') !== nextPlatforms.join(',');

    const nextData = {
      ...data,
      ...(platformsChanged ? { channelConfigsByPlatform: {} } : {}),
      ...(nextPlatforms.includes('wechat') ? { show_duration: false } : {}),
      ...stepData,
    };
    setData(nextData);
    await persistStep(stepData, nextData);
    // The intermediate save may have ADDED platforms to the persisted
    // enabled list (add-ops from this step). Fold them into the wizard's
    // deselection baseline so going back and unchecking one of them
    // produces a remove op on the next save — otherwise the adapter
    // stays enabled while the wizard shows it unselected.
    // Fold ONLY the platforms this wizard step requested to add — the
    // add-list from the wizard's own payload. The server's full enabled
    // response can include platforms other processes enabled concurrently;
    // folding those would misclassify them as wizard deselections on the
    // next save and silently undo the concurrent enablement.
    const wizardRequestedAdds = getPersistableWizardPlatforms(nextData);
    if (wizardRequestedAdds.length > 0) {
      const currentBaseline: string[] = Array.isArray(nextData.__wizardEnabledBaseline)
        ? nextData.__wizardEnabledBaseline
        : [];
      const mergedBaseline = Array.from(new Set([...currentBaseline, ...wizardRequestedAdds]));
      nextData.__wizardEnabledBaseline = mergedBaseline;
      setData({ ...nextData });
    }
    if (currentStep < steps.length - 1) {
      setCurrentStep(currentStep + 1);
    }
  };

  const back = () => {
    if (currentStep > 0) {
      setCurrentStep(currentStep - 1);
    }
  };

  const persistStep = async (stepData: any, mergedData: any) => {
    if (!mergedData) return;
    const platformSelectionOnly =
      Boolean(stepData.platforms || stepData.platform) &&
      !stepData.agents &&
      !stepData.slack &&
      !stepData.discord &&
      !stepData.telegram &&
      !stepData.lark &&
      !stepData.wechat &&
      !stepData.mode &&
      !stepData.channelConfigsByPlatform;
    const shouldPersistConfig =
      !platformSelectionOnly &&
      Boolean(
        mergedData.agents ||
        mergedData.slack ||
        mergedData.discord ||
        mergedData.telegram ||
        mergedData.lark ||
        mergedData.wechat ||
        mergedData.mode ||
        mergedData.platforms ||
        mergedData.platform ||
        mergedData.channelConfigsByPlatform
    );
    if (shouldPersistConfig) {
      await api.saveConfig(
        buildConfigPayload(mergedData, getPersistableWizardPlatforms(mergedData))
      );
    }
    const discordGuildAllowlist = stepData?.discordGuildAllowlist;
    if (
      Array.isArray(discordGuildAllowlist) &&
      (discordGuildAllowlist.length > 0 || stepData?.discordGuildAllowlistTouched === true)
    ) {
      await api.saveSettings({
        guilds: Object.fromEntries(
          discordGuildAllowlist.map((guildId: string) => [guildId, { enabled: true }])
        ),
      }, 'discord');
    }
    if (stepData?.channelConfigsByPlatform) {
      const platforms = Object.keys(stepData.channelConfigsByPlatform);
      for (const p of platforms) {
        const channelConfigs = stepData.channelConfigsByPlatform[p];
        if (channelConfigs && Object.keys(channelConfigs).length > 0) {
          await api.saveSettings({ channels: channelConfigs }, p);
        }
      }
    }
  };

  const CurrentComponent = steps[currentStep].component;
  const stepId = steps[currentStep].id;
  const wizardGlowClass =
    stepId === 'welcome' ? 'page-glow-wizard-welcome'
    : stepId === 'agents' ? 'page-glow-wizard-backends'
    : stepId === 'platform' ? 'page-glow-wizard-platforms'
    : stepId === 'platform-slack' ? 'page-glow-wizard-slack'
    : stepId === 'summary' ? 'page-glow-wizard-summary'
    : 'page-glow-wizard-platforms';

  if (!loaded) return <div className="min-h-screen flex items-center justify-center bg-background text-muted">{t('common.loading')}</div>;

  // Welcome step omits the segmented progress and the skip button (matches design.pen Kebr6).
  // Summary step keeps the rail but disables skip (already at the end).
  const isWelcome = stepId === 'welcome';
  const isSummary = stepId === 'summary';
  // Include welcome in the count so the counter ("Step X / N") matches the
  // step eyebrows (03 — PLATFORMS, etc.). The progress bar itself still
  // skips welcome via showProgress to keep the welcome screen clean.
  const progressTotal = steps.length;
  const progressIndex = steps.findIndex((step) => step.id === stepId);

  return (
    <div
      className={clsx(
        // The mobile body is scroll-locked (index.css @media max-width:767px:
        // html/body overflow-hidden) for the iOS keyboard fix. The wizard
        // bypasses AppShell, so — like AppShell's <main> — it must be its own
        // internal scroll container on phones, or tall steps strand the footer
        // button below the fold. Desktop keeps normal document flow.
        'h-[var(--app-shell-h)] overflow-y-auto px-5 py-7 text-foreground md:h-auto md:min-h-screen md:overflow-visible md:px-10 md:py-10',
        wizardGlowClass
      )}
    >
      <div className="mx-auto flex min-h-full max-w-[1280px] flex-col gap-8 md:min-h-[calc(100vh-4rem)]">
        <WizardChrome
          current={Math.max(0, progressIndex)}
          total={Math.max(progressTotal, 1)}
          showProgress={!isWelcome}
          onSkip={!isWelcome && !isSummary ? () => setCurrentStep(steps.length - 1) : undefined}
        />

        <div className="flex flex-1 flex-col items-center justify-start">
          <AnimatePresence mode="wait">
            <motion.div
              key={currentStep}
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.18 }}
              className="w-full"
            >
              <CurrentComponent
                data={data}
                onNext={next}
                onBack={back}
                isFirst={currentStep === 0}
                isLast={currentStep === steps.length - 1}
              />
            </motion.div>
          </AnimatePresence>
        </div>
      </div>
    </div>
  );
};
