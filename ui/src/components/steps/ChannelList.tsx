import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeft,
  ArrowRight,
  AtSign,
  CheckSquare,
  ChevronDown,
  ChevronUp,
  Globe,
  Hash,
  HelpCircle,
  MessageSquare,
  RefreshCw,
  Square,
  Trash2,
  Users,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { DirectoryBrowser } from '../ui/directory-browser';
import { useNavigate } from 'react-router-dom';
import clsx from 'clsx';
import { getEnabledPlatforms, platformSupportsChannels } from '../../lib/platforms';
import { hasUsableSecret } from '../../lib/secretFields';
import { EyebrowBadge, PlatformIcon, WizardCard } from '../visual';
import { RoutingConfigPanel } from '../shared/RoutingConfigPanel';
import { CompactSelect, SearchField, ToggleSwitch } from '../settings/SettingsPrimitives';
import { Button } from '../ui/button';

const PLATFORM_BRAND_COLORS: Record<string, string> = {
  slack: '#4A154B',
  discord: '#5865F2',
  telegram: '#0088CC',
  lark: '#4C6EB5',
  wechat: '#07C160',
};

const platformBoxStyle = (platformId: string): React.CSSProperties => {
  const base = PLATFORM_BRAND_COLORS[platformId] || '#5BFFA0';
  return { backgroundColor: `${base}26`, borderColor: `${base}66` };
};

interface ChannelListProps {
  data?: any;
  onNext?: (data: any) => void;
  onBack?: () => void;
  isPage?: boolean;
  forcedPlatform?: string;
  /** When set in wizard mode, show platform tabs to switch between platforms in a single step */
  wizardPlatforms?: string[];
}

interface ChannelConfig {
  enabled: boolean;
  show_message_types: string[];
  custom_cwd: string;
  routing: {
    agent_name?: string | null;
    model?: string | null;
    reasoning_effort?: string | null;
    opencode_agent?: string | null;
    opencode_model?: string | null;
    opencode_reasoning_effort?: string | null;
    claude_agent?: string | null;
    claude_model?: string | null;
    claude_reasoning_effort?: string | null;
    codex_agent?: string | null;
    codex_model?: string | null;
    codex_reasoning_effort?: string | null;
  };
  require_mention?: boolean | null;  // null=use global default, true=require, false=don't require
  require_bind?: boolean | null;  // null/false=off, true=only bound users may use this channel
}

interface TelegramDiscoverySummary {
  discovered_count: number;
  visible_count: number;
  hidden_private_count: number;
  forum_count: number;
}

interface ChannelRefreshMeta {
  refreshing?: boolean;
  last_success_at?: string | null;
  last_attempt_at?: string | null;
  error?: string | null;
}

const getDiscordGuildAllowlist = (source: any): string[] => {
  const allowlist = source?.discordGuildAllowlist || source?.guild_allowlist || source?.discord?.guild_allowlist;
  return Array.isArray(allowlist) ? allowlist : [];
};

const buildDiscordGuildSettings = (allowlist: string[]) =>
  Object.fromEntries(allowlist.map((guildId) => [guildId, { enabled: true }]));

const addDiscordGuildToAllowlist = (allowlist: string[], selectedGuild: string): string[] => {
  const merged = [...allowlist];
  if (selectedGuild && !merged.includes(selectedGuild)) {
    merged.push(selectedGuild);
  }
  return merged;
};

export const ChannelList: React.FC<ChannelListProps> = ({ data = {}, onNext, onBack, isPage, forcedPlatform, wizardPlatforms }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [loading, setLoading] = useState(false);
  const [channels, setChannels] = useState<any[]>([]);
  const [browseAll, setBrowseAll] = useState(false);
  const [loadingAll, setLoadingAll] = useState(false);
  const channelLoadingCountsRef = useRef({ default: 0, all: 0 });
  const allPlatformsLoadingCountRef = useRef(0);
  // Wizard multi-platform mode: show tabs instead of separate steps
  const isWizardMultiPlatform = !isPage && Array.isArray(wizardPlatforms) && wizardPlatforms.length > 1;
  const [wizardActivePlatform, setWizardActivePlatform] = useState(forcedPlatform || wizardPlatforms?.[0] || 'slack');
  const [wizardConfigsMap, setWizardConfigsMap] = useState<Record<string, Record<string, ChannelConfig>>>({});
  const scopedInitialPlatform = forcedPlatform || wizardPlatforms?.[0] || data.platform || 'slack';
  const [configs, setConfigs] = useState<Record<string, ChannelConfig>>(
    data.channelConfigsByPlatform?.[scopedInitialPlatform] || data.channelConfigs || {}
  );
  const [config, setConfig] = useState<any>(data);
  const [pagePlatform, setPagePlatform] = useState<string>(forcedPlatform || data.platform || 'slack');
  const [opencodeOptionsByCwd, setOpencodeOptionsByCwd] = useState<Record<string, any>>({});
  const [claudeAgentsByCwd, setClaudeAgentsByCwd] = useState<Record<string, { id: string; name: string; path: string; source?: string }[]>>({});
  const [codexAgentsByCwd, setCodexAgentsByCwd] = useState<Record<string, { id: string; name: string; path: string; source?: string; description?: string }[]>>({});
  const [claudeModels, setClaudeModels] = useState<string[]>([]);
  const [claudeModelLabels, setClaudeModelLabels] = useState<Record<string, string>>({});
  const [claudeReasoningOptions, setClaudeReasoningOptions] = useState<Record<string, { value: string; label: string }[]>>({});
  const [codexModels, setCodexModels] = useState<string[]>([]);
  const [guilds, setGuilds] = useState<any[]>([]);
  const [selectedGuildIds, setSelectedGuildIds] = useState<string[]>(getDiscordGuildAllowlist(data));
  const [selectedGuild, setSelectedGuild] = useState<string>(getDiscordGuildAllowlist(data)[0] || '');
  const selectedGuildIdsRef = useRef<string[]>(getDiscordGuildAllowlist(data));
  const confirmedGuildAllowlistRef = useRef<string[]>(getDiscordGuildAllowlist(data));
  const allowlistSaveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const allowlistSaveVersionRef = useRef(0);
  const configSaveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const configVersionRef = useRef(0);
  const configRef = useRef<any>(data);
  const [telegramSummary, setTelegramSummary] = useState<TelegramDiscoverySummary | null>(null);
  const [refreshMetaByPlatform, setRefreshMetaByPlatform] = useState<Record<string, ChannelRefreshMeta>>({});
  const refreshFollowupTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const refreshFollowupVersionRef = useRef(0);
  const channelRequestVersionRef = useRef(0);
  const allPlatformsRequestVersionRef = useRef(0);
  // Directory browser state — tracks which channel's cwd picker is open
  const [browsingCwdFor, setBrowsingCwdFor] = useState<string | null>(null);
  // Page-mode tab/search/collapse state (only used when isPage is true)
  const [pageTab, setPageTab] = useState<string>('all');
  const [searchQuery, setSearchQuery] = useState('');
  const [expandedChannelId, setExpandedChannelId] = useState<string | null>(null);
  const [showInactive, setShowInactive] = useState(true);
  // When true, also fetch channels the platform no longer returns (deleted /
  // inaccessible) so the user can review and remove them.
  const [showUnavailable, setShowUnavailable] = useState(false);
  const [removingChannelId, setRemovingChannelId] = useState<string | null>(null);
  // All-channels aggregation: keyed by platform
  const [allChannelsByPlatform, setAllChannelsByPlatform] = useState<Record<string, any[]>>({});
  const [allConfigsByPlatform, setAllConfigsByPlatform] = useState<Record<string, Record<string, ChannelConfig>>>({});
  const [allLoading, setAllLoading] = useState(false);
  const vibeAgents = config.agent_catalog?.agents || [];
  const defaultAgentName = config.agent_catalog?.default_agent_name || null;
  const agentByName = useMemo(
    () => Object.fromEntries(vibeAgents.map((agent: any) => [agent.name, agent])),
    [vibeAgents]
  );

  useEffect(() => {
    return () => {
      Object.values(refreshFollowupTimersRef.current).forEach((timer) => clearTimeout(timer));
    };
  }, []);

  const applySelectedGuildIds = (allowlist: string[]) => {
    const normalized = [...allowlist];
    selectedGuildIdsRef.current = normalized;
    setSelectedGuildIds(normalized);
  };

  const applyConfig = (nextConfig: any): number => {
    configRef.current = nextConfig;
    configVersionRef.current += 1;
    setConfig(nextConfig);
    return configVersionRef.current;
  };

  const saveLatestConfig = async (): Promise<boolean> => {
    const saveTask = configSaveQueueRef.current.then(async () => {
      await api.saveConfig(configRef.current);
    });
    configSaveQueueRef.current = saveTask.catch(() => {});
    try {
      await saveTask;
      return true;
    } catch {
      return false;
    }
  };

  useEffect(() => {
    configRef.current = config;
  }, [config]);

  useEffect(() => {
    if (isPage) return;
    let cancelled = false;

    const targetPlatform = isWizardMultiPlatform ? wizardActivePlatform : (forcedPlatform || data.platform);
    const loadWizardPlatformSettings = async () => {
      // If we already have locally saved configs for this platform, use them
      if (isWizardMultiPlatform && wizardConfigsMap[targetPlatform]) {
        if (!cancelled) setConfigs(wizardConfigsMap[targetPlatform]);
        return;
      }
      try {
        const settings = await api.getSettings(targetPlatform);
        if (!cancelled) {
          setConfig((current: any) => ({ ...current, agent_catalog: settings.agent_catalog || current.agent_catalog }));
          setConfigs(settings.channels || {});
        }
      } catch {
        if (!cancelled) {
          // Fallback to wizard-local state if API fetch fails.
          setConfigs(data.channelConfigsByPlatform?.[targetPlatform || 'slack'] || data.channelConfigs || {});
        }
      }
    };

    loadWizardPlatformSettings();
    return () => {
      cancelled = true;
    };
  }, [isPage, data.platform, data.channelConfigsByPlatform, forcedPlatform]);

  useEffect(() => {
    if (!isPage) {
      const allowlist = getDiscordGuildAllowlist(data);
      confirmedGuildAllowlistRef.current = allowlist;
      applySelectedGuildIds(allowlist);
      setSelectedGuild(allowlist[0] || '');
    }
  }, [data.discordGuildAllowlist, data.discord?.guild_allowlist, isPage]);


  useEffect(() => {
    if (isPage) {
      api.getConfig().then(c => {
        applyConfig(c);
        const allowlist = getDiscordGuildAllowlist(c);
        confirmedGuildAllowlistRef.current = allowlist;
        applySelectedGuildIds(allowlist);
        setSelectedGuild(allowlist[0] || '');
        const defaultPlatform = forcedPlatform || getEnabledPlatforms(c).find((p) => platformSupportsChannels(c, p)) || c?.platform || 'slack';
        setPagePlatform(defaultPlatform);
        api.getSettings(defaultPlatform).then(s => {
          setConfig((current: any) => ({ ...current, agent_catalog: s.agent_catalog || current.agent_catalog }));
          setConfigs(s.channels || {});
          if (defaultPlatform === 'discord') {
            const allowlist = getDiscordGuildAllowlist(s);
            confirmedGuildAllowlistRef.current = allowlist;
            applySelectedGuildIds(allowlist);
            setSelectedGuild(allowlist[0] || '');
          }
        });
      });
    }
  }, [forcedPlatform, isPage]);

  const platform = isWizardMultiPlatform
    ? wizardActivePlatform
    : (forcedPlatform || pagePlatform || config.platform || data.platform || 'slack');
  const channelPlatforms = getEnabledPlatforms(config).filter((p) => platformSupportsChannels(config, p));
  const knownDiscordGuilds = [
    ...guilds,
    ...selectedGuildIds
      .filter((id) => !guilds.some((guild) => guild.id === id))
      .map((id) => ({ id, name: id })),
  ];

  // Switch platforms in wizard multi-platform mode
  const switchWizardPlatform = (newPlatform: string) => {
    if (newPlatform === wizardActivePlatform) return;
    // Save current platform's configs to map
    setWizardConfigsMap(prev => ({ ...prev, [wizardActivePlatform]: configs }));
    // Load new platform's configs from map, then from data, then empty
    const saved = wizardConfigsMap[newPlatform];
    setConfigs(saved || data.channelConfigsByPlatform?.[newPlatform] || {});
    setWizardActivePlatform(newPlatform);
    setChannels([]);
    setBrowseAll(false);
    setTelegramSummary(null);
  };

  useEffect(() => {
    if (!isPage) return;
    if (!platform) return;
    api.getSettings(platform).then((settings) => {
      setConfigs(settings.channels || {});
      if (platform === 'discord') {
        const allowlist = getDiscordGuildAllowlist(settings);
        confirmedGuildAllowlistRef.current = allowlist;
        applySelectedGuildIds(allowlist);
        setSelectedGuild((current) => current || allowlist[0] || '');
      }
    }).catch(() => {});
  }, [api, isPage, platform]);
  const botToken = platform === 'discord'
    ? (config.discord?.bot_token || data.discord?.bot_token || '')
    : platform === 'telegram'
      ? (config.telegram?.bot_token || data.telegram?.bot_token || '')
    : platform === 'lark'
      ? '' // Lark uses app_id + app_secret, not bot_token
      : (config.slack?.bot_token || config.slackBotToken || '');
  const larkAppId = config.lark?.app_id || data.lark?.app_id || '';
  const larkAppSecret = config.lark?.app_secret || data.lark?.app_secret || '';
  const larkDomain = config.lark?.domain || data.lark?.domain || 'feishu';
  const hasChannelCredentials = (platformId: string, source: any = config) => {
    if (platformId === 'lark') {
      return Boolean(source.lark?.app_id && hasUsableSecret(source.lark, 'app_secret'));
    }
    if (platformId === 'slack') return hasUsableSecret(source.slack, 'bot_token', source.slackBotToken);
    if (platformId === 'discord') return hasUsableSecret(source.discord, 'bot_token');
    if (platformId === 'telegram') return hasUsableSecret(source.telegram, 'bot_token');
    return false;
  };

  const clearRefreshFollowups = () => {
    refreshFollowupVersionRef.current += 1;
    Object.values(refreshFollowupTimersRef.current).forEach((timer) => clearTimeout(timer));
    refreshFollowupTimersRef.current = {};
  };

  const invalidateChannelContext = () => {
    clearRefreshFollowups();
    channelRequestVersionRef.current += 1;
    allPlatformsRequestVersionRef.current += 1;
  };

  const nextChannelRequestVersion = () => {
    clearRefreshFollowups();
    channelRequestVersionRef.current += 1;
    return channelRequestVersionRef.current;
  };

  const nextAllPlatformsRequestVersion = () => {
    clearRefreshFollowups();
    allPlatformsRequestVersionRef.current += 1;
    return allPlatformsRequestVersionRef.current;
  };

  const beginChannelLoading = (all: boolean) => {
    const key: 'default' | 'all' = all ? 'all' : 'default';
    channelLoadingCountsRef.current[key] += 1;
    if (all) {
      setLoadingAll(true);
    } else {
      setLoading(true);
    }
    return key;
  };

  const endChannelLoading = (key: 'default' | 'all') => {
    channelLoadingCountsRef.current[key] = Math.max(0, channelLoadingCountsRef.current[key] - 1);
    if (key === 'all') {
      if (channelLoadingCountsRef.current.all === 0) setLoadingAll(false);
    } else if (channelLoadingCountsRef.current.default === 0) {
      setLoading(false);
    }
  };

  const beginAllPlatformsLoading = () => {
    allPlatformsLoadingCountRef.current += 1;
    setAllLoading(true);
  };

  const endAllPlatformsLoading = () => {
    allPlatformsLoadingCountRef.current = Math.max(0, allPlatformsLoadingCountRef.current - 1);
    if (allPlatformsLoadingCountRef.current === 0) setAllLoading(false);
  };

  useEffect(() => () => {
    invalidateChannelContext();
  }, [platform, selectedGuild, pageTab, botToken, larkAppId, larkAppSecret, larkDomain]);

  useEffect(() => {
    if (platform !== 'discord') return;
    if (selectedGuild) return;
    const preferredGuild = selectedGuildIdsRef.current[0] || getDiscordGuildAllowlist(data)[0] || '';
    if (preferredGuild) {
      setSelectedGuild(preferredGuild);
    }
  }, [platform, data.discordGuildAllowlist, selectedGuild]);

  const updateSelectedGuild = (guildId: string) => {
    setSelectedGuild(guildId);
  };

  const toggleAllowedGuild = async (guildId: string, checked: boolean) => {
    const current = selectedGuildIdsRef.current;
    const next = checked
      ? addDiscordGuildToAllowlist(current, guildId)
      : current.filter(id => id !== guildId);
    const version = allowlistSaveVersionRef.current + 1;
    allowlistSaveVersionRef.current = version;
    applySelectedGuildIds(next);
    if (!isPage) return;

    const saveTask = allowlistSaveQueueRef.current.then(async () => {
      const saved = await persistDiscordGuildAllowlist(next);
      if (saved) {
        confirmedGuildAllowlistRef.current = next;
      } else if (allowlistSaveVersionRef.current === version) {
        const confirmed = confirmedGuildAllowlistRef.current;
        applySelectedGuildIds(confirmed);
      }
    });
    allowlistSaveQueueRef.current = saveTask.catch(() => {});
    await saveTask;
  };

  const persistDiscordGuildAllowlist = async (allowlist: string[]): Promise<boolean> => {
    if (!isPage) return true;
    try {
      await api.saveSettings({ guilds: buildDiscordGuildSettings(allowlist) }, 'discord');
      showToast(t('common.saved'), 'success');
      return true;
    } catch {
      showToast(t('channelList.settingsSaveFailed'), 'error');
      return false;
    }
  };

  const loadGuilds = async () => {
    if (!botToken && !hasChannelCredentials('discord')) return;
    try {
      const result = await api.discordGuilds(botToken);
      if (result.ok) {
        setGuilds(result.guilds || []);
      }
    } catch (e) {
      console.error('Failed to load guilds:', e);
    }
  };

  const recordRefreshMeta = (platformId: string, result: any) => {
    setRefreshMetaByPlatform((prev) => ({
      ...prev,
      [platformId]: {
        refreshing: Boolean(result.refreshing),
        last_success_at: result.last_success_at || null,
        last_attempt_at: result.last_attempt_at || null,
        error: result.error || null,
      },
    }));
  };

  const scheduleRefreshFollowup = (platformId: string, all?: boolean) => {
    const existing = refreshFollowupTimersRef.current[platformId];
    if (existing) clearTimeout(existing);
    const scheduledVersion = refreshFollowupVersionRef.current;
    refreshFollowupTimersRef.current[platformId] = setTimeout(() => {
      delete refreshFollowupTimersRef.current[platformId];
      if (refreshFollowupVersionRef.current !== scheduledVersion) return;
      void loadChannels(all, false);
    }, 3000);
  };

  const scheduleAllPlatformsRefreshFollowup = () => {
    const timerKey = '__all__';
    const existing = refreshFollowupTimersRef.current[timerKey];
    if (existing) clearTimeout(existing);
    const scheduledVersion = refreshFollowupVersionRef.current;
    refreshFollowupTimersRef.current[timerKey] = setTimeout(() => {
      delete refreshFollowupTimersRef.current[timerKey];
      if (refreshFollowupVersionRef.current !== scheduledVersion) return;
      void loadAllPlatformsData(false);
    }, 3000);
  };

  const loadChannels = async (all?: boolean, force = false) => {
    const requestVersion = nextChannelRequestVersion();
    if (platform === 'lark') {
      if (!larkAppId || !hasChannelCredentials('lark')) return;
    } else if (!botToken && !hasChannelCredentials(platform)) {
      return;
    }
    const isAll = all ?? browseAll;
    const loadingKey = beginChannelLoading(isAll);
    try {
      if (platform === 'lark') {
        const result = await api.larkChats(larkAppId, larkAppSecret, larkDomain, force, showUnavailable);
        if (channelRequestVersionRef.current !== requestVersion) return;
        recordRefreshMeta(platform, result);
        if (result.ok) {
          setChannels(result.channels || []);
          if (result.refreshing) scheduleRefreshFollowup(platform, isAll);
        }
      } else if (platform === 'telegram') {
        const result = await api.telegramChats(false, showUnavailable);
        if (channelRequestVersionRef.current !== requestVersion) return;
        recordRefreshMeta(platform, result);
        if (result.ok) {
          setChannels(result.channels || []);
          setTelegramSummary(result.summary || null);
        }
      } else if (platform === 'discord') {
        if (!selectedGuild) {
          return;
        }
        const result = await api.discordChannels(botToken, selectedGuild, force, showUnavailable);
        if (channelRequestVersionRef.current !== requestVersion) return;
        recordRefreshMeta(platform, result);
        if (result.ok) {
          const filtered = (result.channels || []).filter((c: any) => c.type === 0 || c.type === 5);
          setChannels(filtered);
          if (result.refreshing) scheduleRefreshFollowup(platform, isAll);
        }
      } else {
        const result = await api.slackChannels(botToken, isAll, force, showUnavailable);
        if (channelRequestVersionRef.current !== requestVersion) return;
        recordRefreshMeta(platform, result);
        if (result.ok) {
          setChannels(result.channels || []);
          if (isAll) setBrowseAll(true);
          if (result.refreshing) scheduleRefreshFollowup(platform, isAll);
        }
      }
    } catch (e) {
      console.error('Failed to load channels:', e);
    } finally {
      endChannelLoading(loadingKey);
    }
  };

  const loadOpenCodeOptions = async (cwd: string) => {
    try {
      const result = await api.opencodeOptions(cwd);
      if (result.ok) {
        setOpencodeOptionsByCwd((prev) => ({ ...prev, [cwd]: result.data }));
      }
    } catch (e) {
      console.error('Failed to load OpenCode options:', e);
    }
  };

  const loadClaudeAgents = async (cwd: string) => {
    try {
      const result = await api.claudeAgents(cwd);
      if (result.ok) {
        setClaudeAgentsByCwd((prev) => ({ ...prev, [cwd]: result.agents || [] }));
      }
    } catch (e) {
      console.error('Failed to load Claude agents:', e);
    }
  };

  const loadClaudeModels = async () => {
    try {
      const result = await api.claudeModels();
      if (result.ok) {
        setClaudeModels(result.models || []);
        setClaudeModelLabels(result.model_labels || {});
        setClaudeReasoningOptions(result.reasoning_options || {});
      }
    } catch (e) {
      console.error('Failed to load Claude models:', e);
    }
  };

  const loadCodexModels = async () => {
    try {
      const result = await api.codexModels();
      if (result.ok) {
        setCodexModels(result.models || []);
      }
    } catch (e) {
      console.error('Failed to load Codex models:', e);
    }
  };

  const loadCodexAgents = async (cwd: string) => {
    try {
      const result = await api.codexAgents(cwd);
      if (result.ok) {
        setCodexAgentsByCwd((prev) => ({ ...prev, [cwd]: result.agents || [] }));
      }
    } catch (e) {
      console.error('Failed to load Codex agents:', e);
    }
  };

  useEffect(() => {
    if (platform === 'lark') {
      if (larkAppId && hasChannelCredentials('lark')) {
        loadChannels();
      }
      return;
    }
    if (!botToken && !hasChannelCredentials(platform)) return;
    if (platform === 'discord') {
      loadGuilds();
      if (selectedGuild) {
        loadChannels();
      }
    } else {
      loadChannels();
    }
  }, [
    botToken,
    platform,
    selectedGuild,
    larkAppId,
    larkAppSecret,
    config.slack?.has_bot_token,
    config.discord?.has_bot_token,
    config.telegram?.has_bot_token,
    config.lark?.has_app_secret,
  ]);

  // Reload the inventory shown by the current view. In the All tab the renderer
  // uses the per-platform `channels` state for the currently selected platform
  // and `allChannelsByPlatform` for the rest, so refresh both.
  const reloadCurrentView = async () => {
    if (isPage && pageTab === 'all') {
      await Promise.all([loadAllPlatformsData(false), loadChannels(undefined, false)]);
    } else {
      await loadChannels(undefined, false);
    }
  };

  // Reload when the user toggles "show unavailable" so the request carries the
  // updated include_not_returned flag.
  const showUnavailableInitRef = useRef(true);
  useEffect(() => {
    if (showUnavailableInitRef.current) {
      showUnavailableInitRef.current = false;
      return;
    }
    void reloadCurrentView();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showUnavailable]);

  const handleRemoveChannel = async (rowPlatform: string, channelId: string, channelName?: string) => {
    const confirmed = typeof window === 'undefined'
      || window.confirm(t('channelList.removeChannelConfirm', { name: channelName || channelId }));
    if (!confirmed) return;
    setRemovingChannelId(channelId);
    try {
      const result = await api.deleteChannel(rowPlatform, channelId);
      if (result?.ok) {
        // Drop the removed channel's local settings so a later edit of another
        // channel doesn't re-persist (and thus recreate) the deleted scope.
        if (rowPlatform === platform) {
          setConfigs((prev) => {
            if (!(channelId in prev)) return prev;
            const next = { ...prev };
            delete next[channelId];
            return next;
          });
        }
        setAllConfigsByPlatform((prev) => {
          const platformConfigs = prev[rowPlatform];
          if (!platformConfigs || !(channelId in platformConfigs)) return prev;
          const nextPlatform = { ...platformConfigs };
          delete nextPlatform[channelId];
          return { ...prev, [rowPlatform]: nextPlatform };
        });
        await reloadCurrentView();
      }
    } catch (e) {
      console.error('Failed to remove channel:', e);
    } finally {
      setRemovingChannelId(null);
    }
  };

  useEffect(() => {
    if (config.agents?.claude?.enabled) {
      loadClaudeModels();
    }
  }, [config.agents?.claude?.enabled]);

  useEffect(() => {
    if (config.agents?.codex?.enabled) {
      loadCodexModels();
    }
  }, [config.agents?.codex?.enabled]);

  useEffect(() => {
    if (!channels.length) return;
    const defaultCwd = config.runtime?.default_cwd || '~/work';
    const defaultAgent = agentByName[defaultAgentName || ''] || null;

    const neededOpenCodeCwds = new Set<string>();
    const neededClaudeCwds = new Set<string>();
    const neededCodexCwds = new Set<string>();

    channels.forEach((channel) => {
      const raw = configs[channel.id];
      if (!raw || raw.enabled === false) return;
      const effectiveCwd = (raw.custom_cwd ?? '') || defaultCwd;
      const routing = raw.routing || {};
      const selectedAgent = routing.agent_name ? agentByName[routing.agent_name] : null;
      const backend = selectedAgent?.backend || defaultAgent?.backend || 'opencode';

      if (backend === 'opencode' && config.agents?.opencode?.enabled) {
        neededOpenCodeCwds.add(effectiveCwd);
      }
      if (backend === 'claude' && config.agents?.claude?.enabled) {
        neededClaudeCwds.add(effectiveCwd);
      }
      if (backend === 'codex' && config.agents?.codex?.enabled) {
        neededCodexCwds.add(effectiveCwd);
      }
    });

    neededOpenCodeCwds.forEach((cwd) => {
      if (!opencodeOptionsByCwd[cwd]) {
        void loadOpenCodeOptions(cwd);
      }
    });

    neededClaudeCwds.forEach((cwd) => {
      if (!claudeAgentsByCwd[cwd]) {
        void loadClaudeAgents(cwd);
      }
    });

    neededCodexCwds.forEach((cwd) => {
      if (!codexAgentsByCwd[cwd]) {
        void loadCodexAgents(cwd);
      }
    });
  }, [channels, configs, config.runtime?.default_cwd, config.agents?.opencode?.enabled, config.agents?.claude?.enabled, config.agents?.codex?.enabled, agentByName, defaultAgentName]);

  const isChannelEnabled = (channelId: string) => {
    const channel = configs[channelId];
    return channel ? channel.enabled !== false : false;
  };

  const persistConfigs = async (nextConfigs: Record<string, ChannelConfig>) => {
    if (!isPage) {
      setConfigs(nextConfigs);
      return;
    }

    setLoading(true);
    try {
      await api.saveSettings({ channels: nextConfigs }, platform);
      showToast(t('channelList.settingsSaved'));
    } catch {
      showToast(t('channelList.settingsSaveFailed'), 'error');
    } finally {
      setLoading(false);
    }
  };

  const updateConfig = (channelId: string, patch: Partial<ChannelConfig>) => {
    const base = configs[channelId] || defaultConfig();
    const next = { ...base, ...patch };
    if (!next.show_message_types) {
      next.show_message_types = defaultConfig().show_message_types;
    }
    if (!next.routing || typeof next.routing !== 'object') {
      next.routing = { agent_name: null };
    }
    const nextConfigs = { ...configs, [channelId]: next };
    setConfigs(nextConfigs);
    void persistConfigs(nextConfigs);
  };

  const defaultConfig = (): ChannelConfig => ({
    enabled: false,
    show_message_types: ['assistant'],
    custom_cwd: '',
    routing: {
      agent_name: null,
      model: null,
      reasoning_effort: null,
      opencode_agent: null,
      opencode_model: null,
      opencode_reasoning_effort: null,
      claude_agent: null,
      claude_model: null,
      claude_reasoning_effort: null,
      codex_agent: null,
      codex_model: null,
      codex_reasoning_effort: null,
    },
    require_mention: null,
    require_bind: null,
  });

  const selectedCount = channels.filter((channel) => isChannelEnabled(channel.id)).length;
  const currentRefreshMeta = refreshMetaByPlatform[platform] || {};
  const refreshStatusText = React.useMemo(() => {
    if (currentRefreshMeta.refreshing) return t('channelList.refreshingCache');
    if (currentRefreshMeta.error) return t('channelList.refreshFailed');
    if (currentRefreshMeta.last_success_at) {
      const parsed = new Date(currentRefreshMeta.last_success_at);
      if (!Number.isNaN(parsed.getTime())) {
        return t('channelList.lastSynced', { time: parsed.toLocaleString() });
      }
    }
    return '';
  }, [currentRefreshMeta, t]);

  // ---- Page-mode all-platforms aggregation ----
  const updateConfigForPlatform = async (
    platformId: string,
    channelId: string,
    patch: Partial<ChannelConfig>
  ) => {
    const platformConfigs = (allConfigsByPlatform[platformId] || (platformId === platform ? configs : {})) as Record<
      string,
      ChannelConfig
    >;
    const base = platformConfigs[channelId] || defaultConfig();
    const next = { ...base, ...patch };
    if (!next.show_message_types) {
      next.show_message_types = defaultConfig().show_message_types;
    }
    if (!next.routing || typeof next.routing !== 'object') {
      next.routing = { agent_name: null };
    }
    const nextPlatformConfigs = { ...platformConfigs, [channelId]: next };
    setAllConfigsByPlatform((prev) => ({ ...prev, [platformId]: nextPlatformConfigs }));
    if (platformId === platform) {
      setConfigs(nextPlatformConfigs);
    }
    if (!isPage) return;
    try {
      await api.saveSettings({ channels: nextPlatformConfigs }, platformId);
      showToast(t('channelList.settingsSaved'));
    } catch {
      showToast(t('channelList.settingsSaveFailed'), 'error');
    }
  };

  const loadAllPlatformsData = async (force = false) => {
    if (!isPage) return;
    const platforms = getEnabledPlatforms(config).filter((p) => platformSupportsChannels(config, p));
    if (platforms.length === 0) return;
    const requestVersion = nextAllPlatformsRequestVersion();
    beginAllPlatformsLoading();
    try {
      const results = await Promise.all(
        platforms.map(async (p) => {
          let channelsList: any[] = [];
          let configsMap: Record<string, ChannelConfig> = {};
          let refreshing = false;
          try {
            const settings = await api.getSettings(p);
            configsMap = (settings.channels || {}) as Record<string, ChannelConfig>;
            if (p === 'lark') {
              const appId = config.lark?.app_id || '';
              const appSecret = config.lark?.app_secret || '';
              const domain = config.lark?.domain || 'feishu';
              if (appId && hasChannelCredentials('lark')) {
                const result = await api.larkChats(appId, appSecret, domain, force, showUnavailable);
                recordRefreshMeta(p, result);
                refreshing = Boolean(result.refreshing);
                if (result.ok) channelsList = result.channels || [];
              }
            } else if (p === 'telegram') {
              if (hasChannelCredentials('telegram')) {
                const result = await api.telegramChats(false, showUnavailable);
                if (result.ok) channelsList = result.channels || [];
              }
            } else if (p === 'discord') {
              const allowlist = getDiscordGuildAllowlist(settings);
              const guildId = allowlist[0] || selectedGuildIdsRef.current[0] || selectedGuild;
              const token = config.discord?.bot_token || '';
              if (guildId && hasChannelCredentials('discord')) {
                const result = await api.discordChannels(token, guildId, force, showUnavailable);
                recordRefreshMeta(p, result);
                refreshing = Boolean(result.refreshing);
                if (result.ok) {
                  channelsList = (result.channels || []).filter((c: any) => c.type === 0 || c.type === 5);
                }
              }
            } else if (p === 'slack') {
              if (hasChannelCredentials('slack')) {
                const result = await api.slackChannels(config.slack?.bot_token || '', false, force, showUnavailable);
                recordRefreshMeta(p, result);
                refreshing = Boolean(result.refreshing);
                if (result.ok) channelsList = result.channels || [];
              }
            }
          } catch {
            // ignore individual platform failures
          }
          return { platform: p, channels: channelsList, configs: configsMap, refreshing };
        })
      );
      if (allPlatformsRequestVersionRef.current !== requestVersion) return;
      const channelsByPlatform: Record<string, any[]> = {};
      const configsByPlatform: Record<string, Record<string, ChannelConfig>> = {};
      for (const r of results) {
        channelsByPlatform[r.platform] = r.channels;
        configsByPlatform[r.platform] = r.configs;
      }
      setAllChannelsByPlatform(channelsByPlatform);
      setAllConfigsByPlatform(configsByPlatform);
      if (results.some((r) => r.refreshing)) scheduleAllPlatformsRefreshFollowup();
    } finally {
      endAllPlatformsLoading();
    }
  };

  // Load aggregated data on first All-tab activation, and refresh when config keys change
  useEffect(() => {
    if (!isPage) return;
    if (pageTab !== 'all') return;
    void loadAllPlatformsData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    isPage,
    pageTab,
    config.slack?.bot_token,
    config.slack?.has_bot_token,
    config.discord?.bot_token,
    config.discord?.has_bot_token,
    config.telegram?.bot_token,
    config.telegram?.has_bot_token,
    config.lark?.app_id,
    config.lark?.app_secret,
    config.lark?.has_app_secret,
  ]);

  // When the user picks a per-platform tab, sync pagePlatform so existing flow loads it
  useEffect(() => {
    if (!isPage) return;
    if (pageTab === 'all') return;
    if (pageTab !== pagePlatform) {
      setPagePlatform(pageTab);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPage, pageTab]);

  type AggregatedRow = {
    platform: string;
    channel: any;
    config: ChannelConfig;
  };

  const platformChannelCounts = useMemo(() => {
    const counts: Record<string, { total: number; active: number }> = {};
    const platforms = getEnabledPlatforms(config).filter((p) => platformSupportsChannels(config, p));
    for (const p of platforms) {
      const channelList = p === platform ? channels : allChannelsByPlatform[p] || [];
      const configMap = p === platform ? configs : allConfigsByPlatform[p] || {};
      const total = channelList.length;
      const active = channelList.filter((c: any) => configMap[c.id]?.enabled !== false && configMap[c.id]?.enabled !== undefined && configMap[c.id]?.enabled).length;
      counts[p] = { total, active };
    }
    return counts;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config, platform, channels, configs, allChannelsByPlatform, allConfigsByPlatform]);

  const allTabCounts = useMemo(() => {
    let total = 0;
    let active = 0;
    for (const key of Object.keys(platformChannelCounts)) {
      total += platformChannelCounts[key].total;
      active += platformChannelCounts[key].active;
    }
    return { total, active };
  }, [platformChannelCounts]);

  // Sort channels: enabled channels first
  const sortedChannels = React.useMemo(() => {
    return [...channels].sort((a, b) => {
      const enabledDelta = Number(isChannelEnabled(b.id)) - Number(isChannelEnabled(a.id));
      if (enabledDelta !== 0) return enabledDelta;

      if (platform === 'telegram') {
        const topicDelta = Number(Boolean(b.supports_topics)) - Number(Boolean(a.supports_topics));
        if (topicDelta !== 0) return topicDelta;

        const rank = (channel: any) => {
          if (channel.supports_topics) return 3;
          if (channel.type === 'supergroup') return 2;
          if (channel.type === 'group') return 1;
          return 0;
        };
        const rankDelta = rank(b) - rank(a);
        if (rankDelta !== 0) return rankDelta;

        const timeDelta = (Date.parse(b.last_seen_at || '') || 0) - (Date.parse(a.last_seen_at || '') || 0);
        if (timeDelta !== 0) return timeDelta;
      }

      return String(a.name || a.id).localeCompare(String(b.name || b.id));
    });
  }, [channels, configs, platform]);

  const formatTelegramLastSeen = (value?: string) => {
    if (!value) return '';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return '';
    return parsed.toLocaleString();
  };

  const navigate = useNavigate();

  // WeChat: no channels, redirect to user settings
  if (platform === 'wechat') {
    // In wizard mode, skip channel step entirely
    if (!isPage) {
      return (
        <div className="flex w-full justify-center">
          <WizardCard className="gap-6">
            <div className="space-y-2">
              <EyebrowBadge tone="mint">{t('channelList.eyebrow')}</EyebrowBadge>
              <h2 className="text-[28px] font-bold leading-tight tracking-[-0.4px] text-foreground">
                {t('channelList.title')}
              </h2>
              <p className="text-[14px] leading-[1.55] text-muted">{t('wechat.noChannels')}</p>
            </div>
            <div className="flex flex-col items-center gap-3 rounded-xl border border-cyan/30 bg-cyan/[0.06] px-6 py-10 text-center">
              <div className="flex size-14 items-center justify-center rounded-full border border-cyan/30 bg-cyan/[0.08] text-cyan">
                <MessageSquare size={26} />
              </div>
              <p className="text-[12px] text-muted">{t('wechat.noChannels')}</p>
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
                onClick={() => onNext && onNext({})}
                className="flex-1 sm:flex-none"
              >
                {t('common.continue')}
                <ArrowRight size={14} strokeWidth={2.25} />
              </Button>
            </div>
          </WizardCard>
        </div>
      );
    }

    // In page mode, show notice and link to user settings
    return (
      <div className="max-w-5xl mx-auto flex flex-col h-full">
        <div className="flex justify-between items-center mb-6">
          <div>
            <h2 className="text-3xl font-display font-bold">{t('channelList.title')}</h2>
            <p className="text-muted">{t('wechat.noChannels')}</p>
          </div>
        </div>
        <div className="rounded-2xl border border-border bg-surface-2/40 p-8 text-center shadow-[0_18px_40px_-30px_rgba(0,0,0,0.8)]">
          <div className="w-16 h-16 bg-accent/10 text-accent rounded-full flex items-center justify-center border border-accent/20 mx-auto mb-4">
            <MessageSquare size={32} />
          </div>
          <p className="text-muted mb-6">{t('wechat.noChannels')}</p>
          <Button
            type="button"
            variant="brand"
            size="default"
            onClick={() => navigate('/admin/users')}
          >
            <Users size={18} />
            {t('wechat.manageUserSettings')}
          </Button>
        </div>
      </div>
    );
  }

  // ====================================================================
  // PAGE MODE: redesigned layout (matches design.pen frame A7h8Wv)
  // - Header with title + 240px search + Rescan
  // - Tabs row (All channels + per-platform with PlatformIcon + counts)
  // - Per-tab notes (Discord guild picker, Telegram discovery info)
  // - List header (count summary + Show inactive toggle)
  // - Collapsible channel rows with platform-colored icon box + status badge
  // ====================================================================
  if (isPage) {
    const tabPlatforms = getEnabledPlatforms(config).filter((p) => platformSupportsChannels(config, p));

    const buildRowsForPlatform = (p: string): AggregatedRow[] => {
      const channelList = p === platform ? channels : (allChannelsByPlatform[p] || []);
      const configsMap = p === platform ? configs : (allConfigsByPlatform[p] || {});
      return channelList.map((c: any) => ({
        platform: p,
        channel: c,
        config: configsMap[c.id] || defaultConfig(),
      }));
    };

    const rawRows: AggregatedRow[] = pageTab === 'all'
      ? tabPlatforms.flatMap(buildRowsForPlatform)
      : buildRowsForPlatform(pageTab);

    const lowerQuery = searchQuery.trim().toLowerCase();
    const visibleRows = rawRows
      .filter((r) => {
        const enabled = r.config.enabled === true;
        const isUnavailable = r.channel.visibility_status === 'not_returned';
        // "Show inactive" hides unconfigured rows, but never the unavailable
        // rows the user explicitly opted to review via "Show unavailable".
        if (!showInactive && !enabled && !isUnavailable) return false;
        if (!lowerQuery) return true;
        const name = String(r.channel.name || r.channel.id || '').toLowerCase();
        const id = String(r.channel.id || '').toLowerCase();
        return name.includes(lowerQuery) || id.includes(lowerQuery);
      })
      .sort((a, b) => {
        const aOn = a.config.enabled === true;
        const bOn = b.config.enabled === true;
        if (aOn !== bOn) return aOn ? -1 : 1;
        return String(a.channel.name || a.channel.id).localeCompare(String(b.channel.name || b.channel.id));
      });

    const handleRescan = () => {
      if (pageTab === 'all') {
        void loadAllPlatformsData(true);
      } else {
        void loadChannels(false, true);
      }
    };

    const summary = pageTab === 'all'
      ? { active: allTabCounts.active, total: allTabCounts.total }
      : platformChannelCounts[pageTab] || { active: 0, total: 0 };

    const isRescanLoading = pageTab === 'all' ? allLoading : (loading || loadingAll);

    return (
      <>
        <div className="flex h-full flex-col gap-5">
          {/* Page header — design.pen iXX59 */}
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div className="flex flex-col gap-1.5">
              <h1 className="text-[28px] font-bold leading-tight tracking-[-0.4px] text-foreground">
                {t('channelList.title')}
              </h1>
              <p className="text-[14px] leading-[1.55] text-muted">{t('channelList.subtitle')}</p>
            </div>
            <div className="flex items-center gap-2">
              <SearchField
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder={t('channelList.filterPlaceholder')}
                className="w-full sm:w-[240px]"
              />
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={handleRescan}
                disabled={isRescanLoading}
              >
                <RefreshCw size={14} className={isRescanLoading ? 'animate-spin' : ''} />
                {t('channelList.rescan')}
              </Button>
              {pageTab !== 'all' && refreshStatusText && (
                <span className="text-xs text-muted">{refreshStatusText}</span>
              )}
            </div>
          </div>

          {/* Platform Tabs — design.pen O2hb2M */}
          <div className="flex items-end gap-1 overflow-x-auto border-b border-border">
            <button
              type="button"
              onClick={() => setPageTab('all')}
              className={clsx(
                '-mb-px flex shrink-0 items-center gap-2 border-b-2 px-3 pb-2.5 pt-2 text-[13px] font-medium transition-colors',
                pageTab === 'all'
                  ? 'border-mint text-foreground'
                  : 'border-transparent text-muted hover:text-foreground'
              )}
            >
              <Globe size={16} className={pageTab === 'all' ? 'text-mint' : 'text-muted'} />
              <span>{t('channelList.allChannelsTab')}</span>
              <span
                className={clsx(
                  'rounded-full px-1.5 py-0.5 font-mono text-[10px]',
                  pageTab === 'all' ? 'bg-mint-soft text-mint' : 'bg-foreground/[0.06] text-muted'
                )}
              >
                {allTabCounts.active}
              </span>
            </button>
            {tabPlatforms.map((p) => {
              const counts = platformChannelCounts[p] || { active: 0, total: 0 };
              const active = pageTab === p;
              return (
                <button
                  key={p}
                  type="button"
                  onClick={() => setPageTab(p)}
                  className={clsx(
                    '-mb-px flex shrink-0 items-center gap-2 border-b-2 px-3 pb-2.5 pt-2 text-[13px] font-medium transition-colors',
                    active
                      ? 'border-mint text-foreground'
                      : 'border-transparent text-muted hover:text-foreground'
                  )}
                >
                  <PlatformIcon platform={p} size={16} />
                  <span>{t(`platform.${p}.title`)}</span>
                  <span
                    className={clsx(
                      'rounded-full px-1.5 py-0.5 font-mono text-[10px]',
                      active ? 'bg-mint-soft text-mint' : 'bg-foreground/[0.06] text-muted'
                    )}
                  >
                    {counts.active}
                  </span>
                </button>
              );
            })}
          </div>

          {/* Discord guild picker — only on Discord tab */}
          {pageTab === 'discord' && (
            <div className="rounded-xl border border-border bg-surface-2/40 p-3 text-sm shadow-[0_18px_40px_-30px_rgba(0,0,0,0.8)]">
              <div className="grid gap-3 md:grid-cols-[minmax(220px,280px)_1fr]">
                <div className="space-y-1">
                  <label className="font-medium text-foreground">{t('channelList.guildBrowse')}</label>
                  <CompactSelect
                    value={selectedGuild}
                    onChange={(e) => updateSelectedGuild(e.target.value)}
                    className="w-full"
                  >
                    <option value="">{t('channelList.guildPlaceholder')}</option>
                    {knownDiscordGuilds.map((g) => (
                      <option key={g.id} value={g.id}>{g.name}</option>
                    ))}
                  </CompactSelect>
                </div>
                <div className="space-y-2">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-medium text-foreground">{t('channelList.guildAccess')}</span>
                    <span className="text-xs text-muted">
                      {t('channelList.guildAccessCount', { count: selectedGuildIds.length })}
                    </span>
                  </div>
                  {knownDiscordGuilds.length > 0 && (
                    <div className="flex flex-wrap gap-2">
                      {knownDiscordGuilds.map((g) => (
                        <label
                          key={g.id}
                          className={clsx(
                            'inline-flex items-center gap-2 rounded-lg border px-2 py-1 text-xs transition-colors',
                            selectedGuildIds.includes(g.id)
                              ? 'border-mint/40 bg-mint-soft text-foreground'
                              : 'border-border bg-surface text-muted hover:text-foreground'
                          )}
                        >
                          <input
                            type="checkbox"
                            checked={selectedGuildIds.includes(g.id)}
                            onChange={(e) => toggleAllowedGuild(g.id, e.target.checked)}
                            className="h-3.5 w-3.5 accent-accent"
                          />
                          <span>{g.name}</span>
                        </label>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* Telegram discovery info — only on Telegram tab */}
          {pageTab === 'telegram' && (
            <div className="rounded-xl border border-cyan/30 bg-cyan-soft/20 p-3">
              <div className="text-sm font-medium text-foreground">{t('channelList.telegramDiscoveryTitle')}</div>
              <div className="mt-1 text-sm text-muted">{t('channelList.telegramDiscoveryInfo')}</div>
              <div className="mt-2 text-xs text-muted">
                {t('channelList.telegramDiscoveryStats', {
                  visible: telegramSummary?.visible_count || 0,
                  forum: telegramSummary?.forum_count || 0,
                  hidden: telegramSummary?.hidden_private_count || 0,
                })}
              </div>
            </div>
          )}

          {/* Browse all + Can't find hint — per-platform tabs only */}
          {pageTab !== 'all' && (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-0.5">
              {!['lark', 'telegram'].includes(pageTab) && (
                <Button
                  type="button"
                  variant="secondary"
                  size="xs"
                  onClick={() => loadChannels(true, true)}
                  disabled={loadingAll}
                  className="hover:border-cyan/40"
                >
                  <Globe size={13} className={loadingAll ? 'animate-spin' : ''} />
                  {loadingAll ? t('common.loading') : t('channelList.browseAll')}
                </Button>
              )}
              {browseAll && pageTab === platform && (
                <span className="text-[11px] text-muted">{t('channelList.showingAll')}</span>
              )}
              <span className="group relative inline-flex">
                <span className="flex cursor-help items-center gap-1 text-[12px] text-muted">
                  <HelpCircle size={13} />
                  {t('channelList.cantFindChannel')}
                </span>
                <span className="pointer-events-none absolute bottom-full left-0 z-10 mb-2 w-[min(16rem,calc(100vw-2.5rem))] whitespace-normal rounded bg-text px-3 py-2 text-xs text-bg opacity-0 shadow-lg transition-opacity group-hover:opacity-100">
                  {pageTab === 'discord'
                    ? t('channelList.discordInviteBotHint')
                    : pageTab === 'lark'
                      ? t('channelList.larkInviteBotHint')
                      : pageTab === 'telegram'
                        ? t('channelList.telegramInviteBotHint')
                        : t('channelList.inviteBotHint')}
                </span>
              </span>
            </div>
          )}

          {/* List header — design.pen xQq1G */}
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="text-[13px] text-muted">
              {pageTab === 'all'
                ? t('channelList.headerSummary', { active: summary.active, discovered: summary.total })
                : t('channelList.headerSummaryPlatform', {
                    platform: t(`platform.${pageTab}.title`),
                    active: summary.active,
                    discovered: summary.total,
                  })}
            </div>
            <div className="flex items-center gap-4">
              <label className="inline-flex cursor-pointer items-center gap-2">
                <span className="text-[12px] text-muted">{t('channelList.showUnavailable')}</span>
                <ToggleSwitch
                  enabled={showUnavailable}
                  onClick={() => setShowUnavailable(!showUnavailable)}
                />
              </label>
              <label className="inline-flex cursor-pointer items-center gap-2">
                <span className="text-[12px] text-muted">{t('channelList.showInactive')}</span>
                <ToggleSwitch
                  enabled={showInactive}
                  onClick={() => setShowInactive(!showInactive)}
                />
              </label>
            </div>
          </div>

          {/* Channel rows — design.pen JrNBe / M8FRiG / KywfU */}
          <div className="flex-1 space-y-3 overflow-y-auto">
            {visibleRows.length === 0 && (
              <div className="rounded-xl border border-dashed border-border bg-surface-3/60 p-8 text-center text-muted">
                {searchQuery
                  ? t('channelList.noChannelsForSearch')
                  : pageTab === 'discord' && !selectedGuild
                    ? t('channelList.discordNeedsGuild')
                    : t('channelList.noChannelsLoaded')}
              </div>
            )}
            {visibleRows.map((row) => {
              const channel = row.channel;
              const channelPlatform = row.platform;
              const rawConfig = row.config;
              const def = defaultConfig();
              const defaultAgent = agentByName[defaultAgentName || ''] || null;
              const channelEnabled = rawConfig.enabled === true;
              const isUnavailable = channel.visibility_status === 'not_returned';
              const channelConfig = {
                ...def,
                ...rawConfig,
                enabled: channelEnabled,
                show_message_types: rawConfig.show_message_types || def.show_message_types,
                custom_cwd: rawConfig.custom_cwd ?? def.custom_cwd,
                routing: { ...def.routing, ...(rawConfig.routing || {}) },
                require_mention: rawConfig.require_mention !== undefined ? rawConfig.require_mention : def.require_mention,
                require_bind: rawConfig.require_bind !== undefined ? rawConfig.require_bind : def.require_bind,
              };
              const rowKey = `${channelPlatform}::${channel.id}`;
              const expanded = expandedChannelId === rowKey;
              const selectedAgent = agentByName[channelConfig.routing.agent_name || ''] || agentByName[defaultAgentName || ''];
              const effectiveBackend = selectedAgent?.backend || defaultAgent?.backend || 'opencode';
              const effectiveCwd = channelConfig.custom_cwd || config.runtime?.default_cwd || '~/work';
              const opencodeOptions = opencodeOptionsByCwd[effectiveCwd];
              const claudeAgents = claudeAgentsByCwd[effectiveCwd] || [];
              const codexAgents = codexAgentsByCwd[effectiveCwd] || [];

              const updateRow = (patch: Partial<ChannelConfig>) => {
                void updateConfigForPlatform(channelPlatform, channel.id, patch);
              };

              const backendModel = channelConfig.routing.model || (
                effectiveBackend === 'claude'
                  ? channelConfig.routing.claude_model
                  : effectiveBackend === 'codex'
                    ? channelConfig.routing.codex_model
                    : channelConfig.routing.opencode_model
              );
              const agentSummary = selectedAgent
                ? `${selectedAgent.name}${selectedAgent.model ? ` / ${selectedAgent.model}` : ''}`
                : `${effectiveBackend === 'claude' ? 'Claude' : effectiveBackend === 'codex' ? 'Codex' : 'OpenCode'}${backendModel ? ` / ${backendModel}` : ''}`;

              return (
                <div
                  key={rowKey}
                  className={clsx(
                    'rounded-xl border transition-colors',
                    expanded
                      ? 'border-mint/30 bg-surface-2/70 shadow-[0_0_32px_-8px_rgba(91,255,160,0.45)]'
                      : 'border-border bg-background hover:border-border-strong'
                  )}
                >
                  {/* Top row */}
                  <div
                    role="button"
                    tabIndex={0}
                    onClick={() => setExpandedChannelId(expanded ? null : rowKey)}
                    onKeyDown={(e) => {
                      if (e.target !== e.currentTarget) return;
                      if (e.key === ' ' || e.key === 'Enter') {
                        e.preventDefault();
                        setExpandedChannelId(expanded ? null : rowKey);
                      }
                    }}
                    className="flex w-full cursor-pointer items-center gap-3.5 px-5 py-3.5 text-left"
                  >
                    {/* Toggle */}
                    <span onClick={(e) => e.stopPropagation()}>
                      <ToggleSwitch
                        enabled={channelEnabled}
                        onClick={() => updateRow({ enabled: !channelEnabled })}
                      />
                    </span>

                    {/* Platform-tinted icon box */}
                    <span
                      className="flex size-[34px] shrink-0 items-center justify-center rounded-md border"
                      style={platformBoxStyle(channelPlatform)}
                    >
                      <PlatformIcon platform={channelPlatform} size={18} />
                    </span>

                    {/* Name + meta */}
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-mono text-[13px] font-semibold text-foreground">
                        {channel.name || channel.id}
                      </span>
                      <span className="block truncate text-[11px] text-muted">
                        {channelEnabled
                          ? `${agentSummary} · ID: ${channel.id}`
                          : `ID: ${channel.id}`}
                      </span>
                    </span>

                    {/* Status badge */}
                    {isUnavailable ? (
                      <span
                        className="inline-flex items-center gap-1.5 rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-500"
                        title={channel.metadata?.last_missing_at ? t('channelList.unavailableSince', { at: channel.metadata.last_missing_at }) : undefined}
                      >
                        <span className="size-1.5 rounded-full bg-amber-500" />
                        {t('channelList.unavailableBadge')}
                      </span>
                    ) : (
                      <span
                        className={clsx(
                          'inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium',
                          channelEnabled
                            ? 'border-mint/40 bg-mint-soft text-mint'
                            : 'border-border bg-foreground/[0.04] text-muted'
                        )}
                      >
                        <span
                          className={clsx(
                            'size-1.5 rounded-full',
                            channelEnabled ? 'bg-mint shadow-[0_0_6px_rgba(91,255,160,0.7)]' : 'bg-muted'
                          )}
                        />
                        {channelEnabled ? t('common.enabled') : t('common.disabled')}
                      </span>
                    )}

                    {/* Remove (only for channels no longer on the platform) */}
                    {isUnavailable && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          void handleRemoveChannel(channelPlatform, channel.id, channel.name);
                        }}
                        disabled={removingChannelId === channel.id}
                        className="inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-[11px] font-medium text-muted hover:border-red-500/50 hover:text-red-500 disabled:opacity-50"
                        title={t('channelList.removeChannel')}
                      >
                        <Trash2 size={12} />
                        {t('channelList.removeChannel')}
                      </button>
                    )}

                    {/* Chevron */}
                    {expanded ? (
                      <ChevronUp size={18} className="shrink-0 text-muted" />
                    ) : (
                      <ChevronDown size={18} className="shrink-0 text-muted" />
                    )}
                  </div>

                  {/* Expanded body — design.pen asPXu (VR/RoutingConfig) — shared with /users */}
                  {expanded && (
                    <RoutingConfigPanel
                      value={channelConfig}
                      onChange={(patch) => updateRow(patch)}
                      onBrowseDirectory={() => setBrowsingCwdFor(rowKey)}
                      globalConfig={config}
                      vibeAgents={vibeAgents}
                      defaultAgentName={defaultAgentName}
                      showRequireMention={true}
                      inheritsFromKey={channelPlatform}
                      opencodeOptions={opencodeOptions}
                      claudeAgents={claudeAgents}
                      claudeModels={claudeModels}
                      claudeModelLabels={claudeModelLabels}
                      claudeReasoningOptions={claudeReasoningOptions}
                      codexAgents={codexAgents}
                      codexModels={codexModels}
                    />
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* Directory browser modal — page mode keys by `${platform}::${channelId}` */}
        {browsingCwdFor && (() => {
          const sep = browsingCwdFor.indexOf('::');
          if (sep < 0) return null;
          const browsePlatform = browsingCwdFor.slice(0, sep);
          const browseChannelId = browsingCwdFor.slice(sep + 2);
          const platformConfigs = browsePlatform === platform ? configs : (allConfigsByPlatform[browsePlatform] || {});
          return (
            <DirectoryBrowser
              initialPath={platformConfigs[browseChannelId]?.custom_cwd || config.runtime?.default_cwd || '~/work'}
              onSelect={(path) => {
                void updateConfigForPlatform(browsePlatform, browseChannelId, { custom_cwd: path });
                setBrowsingCwdFor(null);
              }}
              onClose={() => setBrowsingCwdFor(null)}
            />
          );
        })()}
      </>
    );
  }

  const Wrapper: React.FC<{ children: React.ReactNode }> = ({ children }) =>
    isPage ? (
      <div className="flex h-full flex-col mx-auto max-w-6xl">{children}</div>
    ) : (
      <div className="flex w-full justify-center">
        <WizardCard className="gap-6">{children}</WizardCard>
      </div>
    );

  return (
    <>
    <Wrapper>
      <div className={clsx('space-y-4', !isPage && 'space-y-2')}>
        <div>
          {!isPage && <EyebrowBadge tone="mint">{t('channelList.eyebrow')}</EyebrowBadge>}
          <h2
            className={clsx(
              'font-bold tracking-[-0.4px] text-foreground',
              isPage ? 'text-3xl font-semibold tracking-tight' : 'mt-2 text-[28px] leading-tight'
            )}
          >
            {t('channelList.title')}
          </h2>
          <p className={clsx('text-muted', isPage ? '' : 'mt-2 max-w-[560px] text-[14px] leading-[1.55]')}>
            {t('channelList.subtitle')}
          </p>
        </div>

        {isPage && (
          <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-border bg-surface-2/40 px-4 py-3 text-sm text-muted shadow-[0_18px_40px_-30px_rgba(0,0,0,0.8)]">
            <span className="rounded-full border border-mint/40 bg-mint-soft px-2.5 py-1 text-xs font-medium text-mint">
              {t('channelList.enabledCount', { count: selectedCount })}
            </span>
            <span>{t('dashboard.metricGroupsHint', { count: channels.length })}</span>
            <span className="hidden h-1 w-1 rounded-full bg-border md:inline-block" />
            <span className="font-mono text-xs uppercase tracking-[0.18em]">{t(`platform.${platform}.title`)}</span>
          </div>
        )}
      </div>

      {((isPage && channelPlatforms.length > 1) || isWizardMultiPlatform) && (
        <div className="flex flex-wrap gap-2">
          {(isWizardMultiPlatform ? wizardPlatforms! : channelPlatforms).map((candidate) => (
            <button
              key={candidate}
              type="button"
              onClick={() => isWizardMultiPlatform ? switchWizardPlatform(candidate) : setPagePlatform(candidate)}
              className={clsx(
                'rounded-full border px-3 py-1.5 text-[12px] font-medium transition-colors',
                platform === candidate
                  ? 'border-mint/50 bg-mint/[0.16] text-mint shadow-[0_0_20px_-4px_rgba(91,255,160,0.4)]'
                  : 'border-border bg-foreground/[0.04] text-foreground hover:border-border-strong'
              )}
            >
              {t(`platform.${candidate}.title`)}
            </button>
          ))}
        </div>
      )}

      {/* Platform-level require @mention toggle (page mode only) */}
      {isPage && (
        <div className="mb-4 flex flex-wrap items-center justify-between gap-x-3 gap-y-2 rounded-xl border border-border bg-surface-2/40 p-3 shadow-[0_18px_40px_-30px_rgba(0,0,0,0.8)]">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
            <AtSign size={14} className="text-accent" />
            <span className="font-medium text-foreground">{t('dashboard.requireMention')}</span>
            <span className="text-xs text-muted">{t('dashboard.requireMentionHint')}</span>
          </div>
          <button
            onClick={async () => {
              const key = platform as 'slack' | 'discord' | 'telegram' | 'lark' | 'wechat';
              const currentConfig = configRef.current;
              const current = !!(currentConfig as any)[key]?.require_mention;
              const updated = {
                ...currentConfig,
                [key]: { ...(currentConfig as any)[key], require_mention: !current },
              };
              const version = applyConfig(updated);
              const saved = await saveLatestConfig();
              if (saved) {
                showToast(t('common.saved'), 'success');
              } else if (configVersionRef.current === version) {
                applyConfig(currentConfig);
                showToast(t('channelList.settingsSaveFailed'), 'error');
              } else {
                showToast(t('channelList.settingsSaveFailed'), 'error');
              }
            }}
            className={clsx(
              'relative inline-flex h-6 w-11 items-center rounded-full transition-colors',
              (config as any)[platform]?.require_mention ? 'bg-accent' : 'bg-border-strong'
            )}
          >
            <span
              className={clsx(
                'inline-block h-4 w-4 rounded-full bg-background transition-transform shadow-sm',
                (config as any)[platform]?.require_mention ? 'translate-x-6' : 'translate-x-1'
              )}
            />
          </button>
        </div>
      )}

      <div className="mb-4 space-y-3 rounded-2xl border border-border bg-surface-2/40 p-4 shadow-[0_18px_40px_-30px_rgba(0,0,0,0.8)]">
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <Button
              type="button"
              variant="secondary"
              size="sm"
              onClick={() => loadChannels(browseAll, true)}
              className="hover:border-cyan/40"
            >
              <RefreshCw size={14} className={loading ? 'animate-spin' : ''} /> {t('channelList.refreshList')}
            </Button>
            {!browseAll && !['lark', 'telegram'].includes(platform) && (
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={() => loadChannels(true, true)}
                disabled={loadingAll}
                className="hover:border-cyan/40"
              >
                <Globe size={14} className={loadingAll ? 'animate-spin' : ''} />
                {loadingAll ? t('common.loading') : t('channelList.browseAll')}
              </Button>
            )}
            {browseAll && (
              <span className="text-xs text-muted">{t('channelList.showingAll')}</span>
            )}
            {refreshStatusText && (
              <span className="text-xs text-muted">{refreshStatusText}</span>
            )}
            <span className="relative group">
              <span className="flex items-center gap-1 text-sm text-muted cursor-help">
                <HelpCircle size={14} />
                {t('channelList.cantFindChannel')}
              </span>
              <span className="absolute bottom-full left-0 mb-2 px-3 py-2 bg-text text-bg text-xs rounded shadow-lg opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none z-10 w-[min(16rem,calc(100vw-2.5rem))] whitespace-normal">
                {platform === 'discord'
                  ? t('channelList.discordInviteBotHint')
                  : platform === 'lark'
                    ? t('channelList.larkInviteBotHint')
                    : platform === 'telegram'
                      ? t('channelList.telegramInviteBotHint')
                      : t('channelList.inviteBotHint')}
              </span>
            </span>
            {channels.length === 0 && !loading && (
              <span className="text-sm text-warning">{t('channelList.noChannelsFound')}</span>
            )}
          </div>
          <span className="text-sm text-muted font-mono">{t('channelList.enabledCount', { count: selectedCount })}</span>
        </div>
        {platform === 'discord' && (
          <div className="rounded-xl border border-border bg-surface-3/60 p-3 text-sm">
            <div className="grid gap-3 md:grid-cols-[minmax(220px,280px)_1fr]">
              <div className="space-y-1">
                <label className="font-medium text-foreground">{t('channelList.guildBrowse')}</label>
                <CompactSelect
                  value={selectedGuild}
                  onChange={(e) => updateSelectedGuild(e.target.value)}
                  className="w-full"
                >
                  <option value="">{t('channelList.guildPlaceholder')}</option>
                  {knownDiscordGuilds.map((g) => (
                    <option key={g.id} value={g.id}>{g.name}</option>
                  ))}
                </CompactSelect>
              </div>
              <div className="space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium text-foreground">{t('channelList.guildAccess')}</span>
                  <span className="text-xs text-muted">
                    {t('channelList.guildAccessCount', { count: selectedGuildIds.length })}
                  </span>
                </div>
                {knownDiscordGuilds.length > 0 && (
                  <div className="flex flex-wrap gap-2">
                    {knownDiscordGuilds.map((g) => (
                      <label
                        key={g.id}
                        className={clsx(
                           'inline-flex items-center gap-2 rounded-lg border px-2 py-1 text-xs transition-colors',
                           selectedGuildIds.includes(g.id)
                             ? 'border-mint/40 bg-mint-soft text-foreground'
                             : 'border-border bg-surface text-muted hover:text-foreground'
                         )}
                      >
                        <input
                          type="checkbox"
                          checked={selectedGuildIds.includes(g.id)}
                          onChange={(e) => toggleAllowedGuild(g.id, e.target.checked)}
                          className="h-3.5 w-3.5 accent-accent"
                        />
                        <span>{g.name}</span>
                      </label>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
        {isPage && (
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span className="text-muted">
              {platform === 'discord' ? t('channelList.accessPolicyDiscord') : t('channelList.accessPolicy')}
            </span>
            <span className="text-xs text-muted">
              {platform === 'discord' ? t('channelList.accessPolicyHintDiscord') : t('channelList.accessPolicyHint')}
            </span>
          </div>
        )}
        {platform === 'telegram' && (
          <div className="rounded-xl border border-cyan/30 bg-cyan-soft/20 p-3">
            <div className="text-sm font-medium text-foreground">{t('channelList.telegramDiscoveryTitle')}</div>
            <div className="mt-1 text-sm text-muted">{t('channelList.telegramDiscoveryInfo')}</div>
            <div className="mt-2 text-xs text-muted">
              {t('channelList.telegramDiscoveryStats', {
                visible: telegramSummary?.visible_count || 0,
                forum: telegramSummary?.forum_count || 0,
                hidden: telegramSummary?.hidden_private_count || 0,
              })}
            </div>
          </div>
        )}
      </div>

      <div className="flex-1 overflow-y-auto rounded-2xl border border-border bg-surface-2/40 p-3 shadow-[0_18px_40px_-30px_rgba(0,0,0,0.8)] space-y-3">
        {!loading && channels.length === 0 && !botToken && platform !== 'lark' && (
          <div className="rounded-xl border border-dashed border-border bg-surface-3/60 p-8 text-center text-muted">
            {t('channelList.addTokenFirst')}
          </div>
        )}
        {!loading && channels.length === 0 && platform === 'telegram' && !!botToken && (
          <div className="rounded-xl border border-dashed border-border bg-surface-3/60 p-8 text-center">
            <div className="text-sm font-medium text-foreground">{t('channelList.telegramDiscoveryEmptyTitle')}</div>
            <div className="mt-2 text-sm text-muted">{t('channelList.telegramDiscoveryEmptyDesc')}</div>
          </div>
        )}
        {sortedChannels.map((channel) => {
          const rawConfig = configs[channel.id] || {};
          const def = defaultConfig();
          const channelConfig = {
            ...def,
            ...rawConfig,
            enabled: isChannelEnabled(channel.id),
            show_message_types: rawConfig.show_message_types || def.show_message_types,
            custom_cwd: rawConfig.custom_cwd ?? def.custom_cwd,
            routing: {
              ...def.routing,
              ...(rawConfig.routing || {}),
            },
            // Preserve require_mention from rawConfig (can be null, true, or false)
            require_mention: rawConfig.require_mention !== undefined ? rawConfig.require_mention : def.require_mention,
            require_bind: rawConfig.require_bind !== undefined ? rawConfig.require_bind : def.require_bind,
          };
          const effectiveCwd = channelConfig.custom_cwd || config.runtime?.default_cwd || '~/work';
          const opencodeOptions = opencodeOptionsByCwd[effectiveCwd];
          const claudeAgents = claudeAgentsByCwd[effectiveCwd] || [];
          const codexAgents = codexAgentsByCwd[effectiveCwd] || [];
          return (
            <div key={channel.id} className="rounded-xl border border-border bg-surface-3/60 p-4 transition-colors hover:border-border-strong hover:bg-surface-2/70">
              <div className="flex items-center justify-between gap-2">
                <div className="flex min-w-0 flex-1 items-center gap-3">
                  <button
                    onClick={() => updateConfig(channel.id, { enabled: !channelConfig.enabled })}
                    className={clsx('shrink-0', channelConfig.enabled ? 'text-accent' : 'text-muted')}
                  >
                    {channelConfig.enabled ? <CheckSquare size={20} /> : <Square size={20} />}
                  </button>
                  <div className="min-w-0">
                    <div className="flex items-center gap-1 font-medium text-foreground">
                      <Hash size={14} className="shrink-0 text-muted" /> <span className="truncate">{channel.name}</span>
                    </div>
                    <div className="truncate text-xs text-muted font-mono">ID: {channel.id}</div>
                    {platform === 'telegram' && (channel.username || channel.last_seen_at) && (
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
                        {channel.username && <span>@{channel.username}</span>}
                        {channel.last_seen_at && (
                          <span>{t('channelList.telegramLastSeen', { time: formatTelegramLastSeen(channel.last_seen_at) })}</span>
                        )}
                      </div>
                    )}
                  </div>
                </div>
                <span
                  className={clsx(
                    'shrink-0 whitespace-nowrap text-xs px-2 py-0.5 rounded-full border',
                    platform === 'discord'
                      ? 'bg-surface text-foreground border-border'
                      : platform === 'telegram'
                        ? channel.supports_topics
                          ? 'bg-accent/10 text-accent border-accent/20'
                          : channel.type === 'supergroup'
                            ? 'bg-success/10 text-success border-success/20'
                            : channel.is_private
                              ? 'bg-warning/10 text-warning border-warning/20'
                              : 'bg-surface text-foreground border-border'
                      : channel.is_private
                        ? 'bg-warning/10 text-warning border-warning/20'
                        : 'bg-success/10 text-success border-success/20'
                  )}
                >
                  {platform === 'discord'
                    ? (channel.type === 5 ? t('channelList.discordNews') : t('channelList.discordText'))
                    : platform === 'telegram'
                      ? channel.supports_topics
                        ? t('channelList.telegramForum')
                        : channel.type === 'supergroup'
                          ? t('channelList.telegramSupergroup')
                          : channel.type === 'group'
                            ? t('channelList.telegramGroup')
                            : channel.is_private
                              ? t('common.private')
                              : t('channelList.telegramChat')
                    : channel.is_private ? t('common.private') : t('common.public')}
                </span>
              </div>

              {channelConfig.enabled && (
                <RoutingConfigPanel
                  value={channelConfig}
                  onChange={(patch) => updateConfig(channel.id, patch)}
                  onBrowseDirectory={() => setBrowsingCwdFor(channel.id)}
                  globalConfig={config}
                  vibeAgents={vibeAgents}
                  defaultAgentName={defaultAgentName}
                  showRequireMention={true}
                  inheritsFromKey={platform}
                  opencodeOptions={opencodeOptions}
                  claudeAgents={claudeAgents}
                  claudeModels={claudeModels}
                  claudeModelLabels={claudeModelLabels}
                  claudeReasoningOptions={claudeReasoningOptions}
                  codexAgents={codexAgents}
                  codexModels={codexModels}
                  containerClass="mt-4 pl-8"
                />
              )}
            </div>
          );
        })}
        {channels.length === 0 && !loading && (
          <div className="p-8 text-center text-muted">
            {t('channelList.noChannelsLoaded')}
          </div>
        )}
      </div>

      {!isPage && (
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
            className="flex-1 sm:flex-none"
            onClick={() => {
              const discordGuildAllowlist = selectedGuildIds;
              if (isWizardMultiPlatform) {
                // Merge configs from all visited platforms
                const allConfigs = { ...wizardConfigsMap, [wizardActivePlatform]: configs };
                onNext && onNext({
                  channelConfigsByPlatform: {
                    ...(data.channelConfigsByPlatform || {}),
                    ...allConfigs,
                  },
                  ...(getEnabledPlatforms(data).includes('discord') ? { discordGuildAllowlist } : {}),
                });
              } else {
                onNext && onNext({
                  channelConfigsByPlatform: {
                    ...(data.channelConfigsByPlatform || {}),
                    [platform]: configs,
                  },
                  settingsPlatform: platform,
                  ...(platform === 'discord' ? { discordGuildAllowlist } : {}),
                });
              }
            }}
          >
            {t('common.continue')}
            <ArrowRight size={14} strokeWidth={2.25} />
          </Button>
        </div>
      )}
    </Wrapper>

    {/* Directory browser modal */}
    {browsingCwdFor && (
      <DirectoryBrowser
        initialPath={configs[browsingCwdFor]?.custom_cwd || config.runtime?.default_cwd || '~/work'}
        onSelect={(path) => {
          updateConfig(browsingCwdFor, { custom_cwd: path });
          setBrowsingCwdFor(null);
        }}
        onClose={() => setBrowsingCwdFor(null)}
      />
    )}
    </>
  );
};
