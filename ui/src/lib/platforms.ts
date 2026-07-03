export type PlatformName = string;

export type PlatformCapabilities = {
  supports_channels?: boolean;
  supports_threads?: boolean;
  supports_buttons?: boolean;
  supports_quick_replies?: boolean;
  supports_message_editing?: boolean;
  markdown_upload_returns_message_id?: boolean;
  quick_reply_single_column?: boolean;
  supports_typing_indicator?: boolean;
  typing_indicator_requires_clear?: boolean;
  typing_indicator_best_effort?: boolean;
  supports_reaction_indicator?: boolean;
  supports_message_indicator?: boolean;
  supports_message_indicator_delete?: boolean;
  preferred_processing_indicator?: string;
  force_preferred_processing_indicator?: boolean;
  supports_toolcall_delivery?: boolean;
};

export type PlatformDescriptor = {
  id: PlatformName;
  config_key?: string;
  title_key?: string;
  description_key?: string;
  credential_fields?: string[];
  capabilities?: PlatformCapabilities;
  // Structural distinction mirrored from the backend registry: real IM
  // transports are 'im', the always-on in-process workbench is 'workbench'.
  // Optional so older payloads (pre-kind) still type-check.
  kind?: 'im' | 'workbench';
};

// The local Vibe Remote web workbench, surfaced as a peer platform so it shares
// scopes / agent_sessions / routing machinery with Slack/Discord/etc. It has no
// remote credentials and no externally-discovered groups, so UI surfaces treat
// it differently (e.g. it shows no group-count summary).
export const WORKBENCH_PLATFORM_ID = 'avibe';

export const isWorkbenchPlatform = (platform: string): boolean =>
  platform === WORKBENCH_PLATFORM_ID;

const LEGACY_FALLBACK_CATALOG: PlatformDescriptor[] = [
  {
    id: 'slack',
    config_key: 'slack',
    title_key: 'platform.slack.title',
    description_key: 'platform.slack.desc',
    credential_fields: ['bot_token'],
    capabilities: {
      supports_channels: true,
      supports_threads: true,
      supports_buttons: true,
      supports_quick_replies: true,
      supports_message_editing: true,
      supports_typing_indicator: true,
      typing_indicator_best_effort: true,
      supports_reaction_indicator: true,
      supports_message_indicator: true,
      preferred_processing_indicator: 'typing',
    },
  },
  {
    id: 'discord',
    config_key: 'discord',
    title_key: 'platform.discord.title',
    description_key: 'platform.discord.desc',
    credential_fields: ['bot_token'],
    capabilities: {
      supports_channels: true,
      supports_threads: true,
      supports_buttons: true,
      supports_quick_replies: true,
      supports_message_editing: true,
      markdown_upload_returns_message_id: true,
      supports_typing_indicator: true,
      supports_reaction_indicator: true,
      supports_message_indicator: true,
      preferred_processing_indicator: 'typing',
    },
  },
  {
    id: 'telegram',
    config_key: 'telegram',
    title_key: 'platform.telegram.title',
    description_key: 'platform.telegram.desc',
    credential_fields: ['bot_token'],
    capabilities: {
      supports_channels: true,
      supports_threads: false,
      supports_buttons: true,
      supports_quick_replies: true,
      supports_message_editing: true,
      markdown_upload_returns_message_id: true,
      quick_reply_single_column: true,
      supports_typing_indicator: true,
      supports_reaction_indicator: true,
      supports_message_indicator: true,
      supports_message_indicator_delete: true,
      preferred_processing_indicator: 'typing',
    },
  },
  {
    id: 'lark',
    config_key: 'lark',
    title_key: 'platform.lark.title',
    description_key: 'platform.lark.desc',
    credential_fields: ['app_id', 'app_secret'],
    capabilities: {
      supports_channels: true,
      supports_threads: true,
      supports_buttons: true,
      supports_quick_replies: true,
      supports_message_editing: true,
      markdown_upload_returns_message_id: true,
      quick_reply_single_column: true,
      supports_reaction_indicator: true,
      supports_message_indicator: true,
      preferred_processing_indicator: 'reaction',
    },
  },
  {
    id: 'wechat',
    config_key: 'wechat',
    title_key: 'platform.wechat.title',
    description_key: 'platform.wechat.desc',
    credential_fields: ['bot_token'],
    capabilities: {
      supports_channels: false,
      supports_threads: false,
      supports_buttons: false,
      supports_quick_replies: false,
      supports_message_editing: false,
      supports_typing_indicator: true,
      typing_indicator_requires_clear: true,
      supports_reaction_indicator: false,
      supports_message_indicator: true,
      preferred_processing_indicator: 'typing',
      force_preferred_processing_indicator: true,
      supports_toolcall_delivery: false,
    },
  },
];

export const getPlatformCatalog = (data: any): PlatformDescriptor[] => {
  const catalog = data?.platform_catalog || data?.platforms_catalog || data?.catalog?.platforms;
  if (Array.isArray(catalog) && catalog.length > 0) {
    return catalog.filter((platform: any): platform is PlatformDescriptor => typeof platform?.id === 'string');
  }
  return LEGACY_FALLBACK_CATALOG;
};

export const getPlatformIds = (data: any): PlatformName[] => getPlatformCatalog(data).map((platform) => platform.id);

// Catalog entries that are real IM transports (excludes the in-process Avibe
// Workbench). The single place IM-list surfaces (wizard / settings) filter the
// workbench out of the selectable platform set — use the structural ``kind``
// when present, falling back to the id-check for older payloads without it.
export const getImPlatforms = (data: any): PlatformDescriptor[] =>
  getPlatformCatalog(data).filter((p) => (p.kind ? p.kind !== 'workbench' : !isWorkbenchPlatform(p.id)));

export const getEnabledPlatforms = (data: any): PlatformName[] => {
  const catalogIds = new Set(getPlatformIds(data));
  const enabled = data?.platforms?.enabled;
  if (Array.isArray(enabled)) {
    const filtered = enabled.filter((platform: string): platform is PlatformName => catalogIds.has(platform));
    // Honor an explicitly empty list (workbench-only). A non-empty list that
    // filters to nothing is malformed → fall through to the legacy fallback.
    if (filtered.length > 0 || enabled.length === 0) return filtered;
  }
  const legacy = data?.platform;
  if (catalogIds.has(legacy)) {
    return [legacy as PlatformName];
  }
  return [getPlatformCatalog(data)[0]?.id || 'slack'];
};

export const getPlatformDescriptor = (data: any, platform: string): PlatformDescriptor | undefined =>
  getPlatformCatalog(data).find((descriptor) => descriptor.id === platform);

export const platformHasCapability = (
  data: any,
  platform: string,
  capability: keyof PlatformCapabilities
): boolean => !!getPlatformDescriptor(data, platform)?.capabilities?.[capability];

export const platformSupportsChannels = (data: any, platform: string): boolean =>
  platformHasCapability(data, platform, 'supports_channels');

export const platformSupportsToolcallDelivery = (data: any, platform: string): boolean =>
  getPlatformDescriptor(data, platform)?.capabilities?.supports_toolcall_delivery !== false;

export const platformHasCredentials = (data: any, platform: string): boolean => {
  const descriptor = getPlatformDescriptor(data, platform);
  const configKey = descriptor?.config_key || platform;
  const credentialFields = descriptor?.credential_fields || [];
  const platformConfig = data?.[configKey];
  if (!platformConfig || credentialFields.length === 0) {
    return false;
  }
  return credentialFields.every((field) => !!platformConfig?.[field] || !!platformConfig?.[`has_${field}`]);
};

export const platformHasRunnableConfig = (data: any, platform: string): boolean => {
  if (platform === 'wechat') {
    return Boolean(data?.wechat);
  }
  return platformHasCredentials(data, platform);
};

export const hasConfiguredPlatformCredentials = (data: any): boolean =>
  getEnabledPlatforms(data).some((platform) => platformHasRunnableConfig(data, platform));
