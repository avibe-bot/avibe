import type React from 'react';
import { Bot, Sparkles, Terminal } from 'lucide-react';
import { BACKEND_CATALOG } from './agentBackendCatalog.generated';

type BackendDescriptor = (typeof BACKEND_CATALOG)[number];
export type AgentBackendId = BackendDescriptor['id'];
export type NativeCliBackend = Extract<BackendDescriptor, { capabilities: { supports_cli: true } }>['id'];
export type WebOAuthBackend = Extract<BackendDescriptor, { capabilities: { supports_web_oauth: true } }>['id'];
export type BackendId = string;
type BackendCapabilities = { [K in keyof BackendDescriptor['capabilities']]: boolean };

export type BackendUiMeta = {
  id: BackendId;
  label: string;
  defaultCli: string | null;
  defaultEnabled: boolean;
  settingsRoute: string;
  descriptionKey: string;
  capabilities: BackendCapabilities;
  agentOrder: number;
  nativeOrder: number;
  publisher: string;
  accent: 'mint' | 'cyan' | 'violet';
  Icon: React.ComponentType<{ size?: number; className?: string }>;
  initials: string;
  blockCls: string;
  glyphCls: string;
  tileCls: string;
  iconCls: string;
};

// Presentation only. Membership, defaults and capabilities come from Python.
const BACKEND_VISUALS = {
  opencode: {
    agentOrder: 1,
    nativeOrder: 2,
    publisher: 'opencode.ai',
    accent: 'cyan',
    Icon: Terminal,
    initials: 'OP',
    blockCls: 'border-mint/40 bg-mint/[0.10] text-mint-ink',
    glyphCls: 'text-mint-ink',
    tileCls: 'bg-violet-soft',
    iconCls: 'text-violet-ink',
  },
  claude: {
    agentOrder: 0,
    nativeOrder: 0,
    publisher: 'Anthropic',
    accent: 'mint',
    Icon: Sparkles,
    initials: 'CL',
    blockCls: 'border-[rgba(217,119,87,0.4)] bg-[rgba(217,119,87,0.10)] text-[#e8a87c]',
    glyphCls: 'text-cyan-ink',
    tileCls: 'bg-cyan-soft',
    iconCls: 'text-cyan-ink',
  },
  codex: {
    agentOrder: 2,
    nativeOrder: 1,
    publisher: 'OpenAI',
    accent: 'violet',
    Icon: Bot,
    initials: 'CO',
    blockCls: 'border-violet/40 bg-violet/[0.10] text-violet-ink',
    glyphCls: 'text-violet-ink',
    tileCls: 'bg-gold',
    iconCls: 'text-gold-foreground',
  },
  avibe: {
    agentOrder: 3,
    nativeOrder: 3,
    publisher: 'Avibe',
    accent: 'mint',
    Icon: Bot,
    initials: 'AV',
    blockCls: 'border-mint/40 bg-mint/[0.10] text-mint-ink',
    glyphCls: 'text-mint-ink',
    tileCls: 'bg-mint-soft',
    iconCls: 'text-mint-ink',
  },
} as const satisfies Record<AgentBackendId, Pick<BackendUiMeta,
  'agentOrder' | 'nativeOrder' | 'publisher' | 'accent' | 'Icon' | 'initials' | 'blockCls' | 'glyphCls' | 'tileCls' | 'iconCls'>>;

export const AGENT_BACKENDS = BACKEND_CATALOG.map((backend) => ({
  ...BACKEND_VISUALS[backend.id],
  id: backend.id,
  label: backend.display_name,
  defaultCli: backend.default_cli,
  defaultEnabled: backend.default_enabled,
  settingsRoute: backend.settings_route,
  descriptionKey: backend.description_key,
  capabilities: backend.capabilities,
}));

export const NATIVE_CLI_BACKENDS = BACKEND_CATALOG
  .filter((backend): backend is Extract<BackendDescriptor, { capabilities: { supports_cli: true } }> =>
    backend.capabilities.supports_cli)
  .map((backend) => backend.id);

export const NATIVE_SETUP_BACKENDS = [...NATIVE_CLI_BACKENDS]
  .sort((left, right) => BACKEND_VISUALS[left].nativeOrder - BACKEND_VISUALS[right].nativeOrder);

export function isNativeCliBackend(id: string): id is NativeCliBackend {
  return (NATIVE_CLI_BACKENDS as readonly string[]).includes(id);
}

export const DEFAULT_BACKEND_ID = 'opencode';

export const AGENT_BACKEND_BY_ID = Object.fromEntries(
  AGENT_BACKENDS.map((backend) => [backend.id, backend]),
) as Record<string, BackendUiMeta>;

export const DEFAULT_AGENT_STATE = Object.fromEntries(
  AGENT_BACKENDS.map((backend) => [
    backend.id,
    {
      enabled: backend.defaultEnabled,
      ...(backend.capabilities.supports_cli ? { cli_path: backend.defaultCli } : {}),
      status: 'unknown',
    },
  ]),
);

export const DEFAULT_NATIVE_AGENT_STATE = Object.fromEntries(
  NATIVE_CLI_BACKENDS.map((id) => [id, DEFAULT_AGENT_STATE[id]]),
);

export function getBackendUiMeta(id: AgentBackendId): (typeof AGENT_BACKENDS)[number];
export function getBackendUiMeta(id: string): BackendUiMeta;
export function getBackendUiMeta(id: string): BackendUiMeta {
  return (
    AGENT_BACKEND_BY_ID[id] || {
      id,
      label: id.replace(/_/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase()),
      defaultCli: null,
      defaultEnabled: false,
      settingsRoute: `/settings/backends/${id}`,
      descriptionKey: `settings.backends.${id}Description`,
      capabilities: {
        supports_cli: false,
        supports_native_sessions: false,
        supports_runtime_refresh: false,
        supports_web_oauth: false,
        supports_install: false,
      },
      agentOrder: Number.MAX_SAFE_INTEGER,
      nativeOrder: Number.MAX_SAFE_INTEGER,
      publisher: '',
      accent: 'mint',
      Icon: Bot,
      initials: id.slice(0, 2).toUpperCase(),
      blockCls: 'border-border bg-surface-2 text-foreground',
      glyphCls: 'text-muted',
      tileCls: 'bg-surface-2',
      iconCls: 'text-muted',
    }
  );
}
