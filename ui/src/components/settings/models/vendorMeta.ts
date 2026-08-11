// Model Hub identity colors are semantic, not vendor branding: channel/kind
// owns source color, while backend identity owns Agent color.
import type React from 'react';
import { Bot, KeyRound, Sparkles, Terminal } from 'lucide-react';

import type { AgentBackend, Source } from './types';

export type Accent = 'mint' | 'gold' | 'cyan' | 'violet' | 'muted';

// Soft-tinted tile + matching icon/dot color per accent. Uses the theme tokens
// (mint/gold/cyan/violet + *-soft) so it tracks Light/Dark automatically.
export const ACCENT_TILE: Record<Accent, string> = {
  mint: 'model-hub-accent-tile--mint',
  gold: 'model-hub-accent-tile--gold',
  cyan: 'model-hub-accent-tile--cyan',
  violet: 'model-hub-accent-tile--violet',
  muted: 'model-hub-accent-tile--neutral',
};

export const ACCENT_ICON: Record<Accent, string> = {
  mint: 'model-hub-accent-ink--mint',
  gold: 'model-hub-accent-ink--gold',
  cyan: 'model-hub-accent-ink--cyan',
  violet: 'model-hub-accent-ink--violet',
  muted: 'model-hub-accent-ink--neutral',
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

export const ACCENT_PILL: Record<Accent, string> = {
  mint: 'model-hub-accent-pill--mint',
  gold: 'model-hub-accent-pill--gold',
  cyan: 'model-hub-accent-pill--cyan',
  violet: 'model-hub-accent-pill--violet',
  muted: 'model-hub-accent-pill--neutral',
};

type IconType = React.ComponentType<{ size?: number; className?: string }>;

export const SOURCE_IDENTITY_ACCENT = {
  native_cli: 'cyan',
  subscription: 'mint',
  api_key: 'muted',
} as const satisfies Record<'native_cli' | Source['kind'], Accent>;

export function sourceAccent(source: Pick<Source, 'kind' | 'supply_channel'>): Accent {
  return source.supply_channel === 'native_cli'
    ? SOURCE_IDENTITY_ACCENT.native_cli
    : SOURCE_IDENTITY_ACCENT[source.kind];
}

export type SourceVisual = { Icon: IconType; accent: Accent };

export function sourceVisual(source: Pick<Source, 'kind' | 'supply_channel'>): SourceVisual {
  return {
    Icon: source.kind === 'subscription' ? Sparkles : KeyRound,
    accent: sourceAccent(source),
  };
}

// ── Agent backends (Agent card rows) ────────────────────────────────────
export type BackendVisual = { Icon: IconType; accent: Accent };

export const BACKEND_IDENTITY_ACCENT = {
  claude: 'cyan',
  codex: 'mint',
  opencode: 'violet',
} as const satisfies Record<AgentBackend, Accent>;

export const BACKEND_ADOPTION_VENDOR_KEY = {
  claude: 'claude',
  codex: 'chatgpt',
  opencode: 'chatgpt',
} as const satisfies Record<AgentBackend, string>;

const BACKEND_ICON: Record<AgentBackend, IconType> = {
  claude: Sparkles,
  codex: Bot,
  opencode: Terminal,
};

const BACKEND_VISUAL: Record<AgentBackend, BackendVisual> = {
  claude: { Icon: BACKEND_ICON.claude, accent: BACKEND_IDENTITY_ACCENT.claude },
  codex: { Icon: BACKEND_ICON.codex, accent: BACKEND_IDENTITY_ACCENT.codex },
  opencode: { Icon: BACKEND_ICON.opencode, accent: BACKEND_IDENTITY_ACCENT.opencode },
};

export function backendVisual(backend: AgentBackend): BackendVisual {
  return BACKEND_VISUAL[backend] ?? { Icon: Bot, accent: 'muted' };
}

// Official endpoints are used only to classify an existing Source as custom.
// Add API key accepts a Base URL directly and intentionally has no vendor picker.
const OFFICIAL_BASE_URLS: Record<string, string> = {
  anthropic: 'https://api.anthropic.com/v1',
  openai: 'https://api.openai.com/v1',
  zhipuai: 'https://open.bigmodel.cn/api/paas/v4',
  kimi: 'https://api.moonshot.cn/v1',
  xai: 'https://api.x.ai/v1',
};

// An api_key points at a custom endpoint when its base URL is set and differs
// from the vendor's official one — covers both vendor='custom' (official = null)
// and an official vendor whose prefilled Base URL was edited to a relay. Shared
// by the 来源 list (SourceRow) and the custom-model source picker.
export function isCustomEndpoint(source: Pick<Source, 'vendor' | 'base_url'>): boolean {
  if (!source.base_url) return false;
  const official = OFFICIAL_BASE_URLS[source.vendor] ?? null;
  return source.base_url !== official;
}
