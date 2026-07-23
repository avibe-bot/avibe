// Visual + vendor metadata for the Model Hub surfaces. Colors follow the V4
// design (design.pen `产品改造 V4 01r/06r/07`): subscriptions carry the brand
// sparkle accent (Claude→mint, ChatGPT→gold); API keys carry a per-vendor key
// accent (Anthropic→violet, 智谱→cyan, custom/relay→gold). Only the icon
// component is reused from `lib/agentBackends`; the V4 accents differ from the
// older backends page on purpose, so they live here as data.
import type React from 'react';
import { Bot, KeyRound, Sparkles, Terminal } from 'lucide-react';

import type { AgentBackend, Source, SourceProtocol } from './types';

export type Accent = 'mint' | 'gold' | 'cyan' | 'violet' | 'muted';

// Soft-tinted tile + matching icon/dot color per accent. Uses the theme tokens
// (mint/gold/cyan/violet + *-soft) so it tracks Light/Dark automatically.
export const ACCENT_TILE: Record<Accent, string> = {
  mint: 'bg-mint-soft',
  gold: 'bg-gold/15',
  cyan: 'bg-cyan-soft',
  violet: 'bg-violet-soft',
  muted: 'bg-surface-2',
};

export const ACCENT_ICON: Record<Accent, string> = {
  mint: 'text-mint',
  gold: 'text-gold',
  cyan: 'text-cyan',
  violet: 'text-violet',
  muted: 'text-muted',
};

// Status dot fill (composite pill · recent-switch list). Gold reserved for the
// "entered metered" billing marker.
export const ACCENT_DOT: Record<Accent, string> = {
  mint: 'bg-mint',
  gold: 'bg-gold',
  cyan: 'bg-cyan',
  violet: 'bg-violet',
  muted: 'bg-muted',
};

type IconType = React.ComponentType<{ size?: number; className?: string }>;

// api_key accent by vendor id. Unknown vendors fall back to violet (the generic
// "key" accent used by the add-source menu).
const API_KEY_ACCENT: Record<string, Accent> = {
  anthropic: 'violet',
  openai: 'gold',
  zhipuai: 'cyan',
  kimi: 'cyan',
  xai: 'muted',
  custom: 'gold',
};

export function sourceAccent(source: Pick<Source, 'kind' | 'vendor'>): Accent {
  if (source.kind === 'subscription') {
    if (source.vendor === 'openai') return 'gold';
    return 'mint'; // anthropic + any other subscription
  }
  return API_KEY_ACCENT[source.vendor] ?? 'violet';
}

export type SourceVisual = { Icon: IconType; accent: Accent };

export function sourceVisual(source: Pick<Source, 'kind' | 'vendor'>): SourceVisual {
  return {
    Icon: source.kind === 'subscription' ? Sparkles : KeyRound,
    accent: sourceAccent(source),
  };
}

// ── Agent backends (Agent card rows) ────────────────────────────────────
export type BackendVisual = { Icon: IconType; accent: Accent };

const BACKEND_VISUAL: Record<AgentBackend, BackendVisual> = {
  claude: { Icon: Sparkles, accent: 'mint' },
  codex: { Icon: Bot, accent: 'gold' },
  opencode: { Icon: Terminal, accent: 'violet' },
};

export function backendVisual(backend: AgentBackend): BackendVisual {
  return BACKEND_VISUAL[backend] ?? { Icon: Bot, accent: 'muted' };
}

// ── API-key vendor picker (frame 06r) ───────────────────────────────────
// value = standard vendor id; labelKey → i18n; base_url prefilled for official
// vendors (editable), null for 自定义. Protocol drives the created source.
export type VendorOption = {
  value: string;
  labelKey: string;
  base_url: string | null;
  protocol: SourceProtocol;
};

export const VENDOR_OPTIONS: VendorOption[] = [
  { value: 'custom', labelKey: 'settings.models.addKey.vendors.custom', base_url: null, protocol: 'openai_compatible' },
  { value: 'anthropic', labelKey: 'settings.models.addKey.vendors.anthropic', base_url: 'https://api.anthropic.com', protocol: 'anthropic' },
  { value: 'openai', labelKey: 'settings.models.addKey.vendors.openai', base_url: 'https://api.openai.com/v1', protocol: 'openai_chat' },
  { value: 'zhipuai', labelKey: 'settings.models.addKey.vendors.zhipuai', base_url: 'https://open.bigmodel.cn/api/paas/v4', protocol: 'openai_compatible' },
  { value: 'kimi', labelKey: 'settings.models.addKey.vendors.kimi', base_url: 'https://api.moonshot.cn/v1', protocol: 'openai_compatible' },
  { value: 'xai', labelKey: 'settings.models.addKey.vendors.xai', base_url: 'https://api.x.ai/v1', protocol: 'openai_chat' },
];

export const DEFAULT_VENDOR = VENDOR_OPTIONS[0];
