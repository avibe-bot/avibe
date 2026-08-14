import type React from 'react';
import { Bot, Sparkles, Terminal } from 'lucide-react';

export type BackendId = 'opencode' | 'claude' | 'codex' | string;

export type BackendUiMeta = {
  id: BackendId;
  label: string;
  defaultCli: string;
  defaultEnabled: boolean;
  settingsRoute: string;
  Icon: React.ComponentType<{ size?: number; className?: string }>;
  initials: string;
  blockCls: string;
  glyphCls: string;
  tileCls: string;
  iconCls: string;
};

export const AGENT_BACKENDS: BackendUiMeta[] = [
  {
    id: 'opencode',
    label: 'OpenCode',
    defaultCli: 'opencode',
    defaultEnabled: true,
    settingsRoute: '/settings/backends/opencode',
    Icon: Terminal,
    initials: 'OP',
    blockCls: 'border-mint/40 bg-mint/[0.10] text-mint-ink',
    glyphCls: 'text-mint-ink',
    tileCls: 'bg-violet-soft',
    iconCls: 'text-violet-ink',
  },
  {
    id: 'claude',
    label: 'Claude Code',
    defaultCli: 'claude',
    defaultEnabled: true,
    settingsRoute: '/settings/backends/claude',
    Icon: Sparkles,
    initials: 'CL',
    blockCls: 'border-[rgba(217,119,87,0.4)] bg-[rgba(217,119,87,0.10)] text-[#e8a87c]',
    glyphCls: 'text-cyan-ink',
    tileCls: 'bg-cyan-soft',
    iconCls: 'text-cyan-ink',
  },
  {
    id: 'codex',
    label: 'Codex',
    defaultCli: 'codex',
    defaultEnabled: false,
    settingsRoute: '/settings/backends/codex',
    Icon: Bot,
    initials: 'CO',
    blockCls: 'border-violet/40 bg-violet/[0.10] text-violet-ink',
    glyphCls: 'text-violet-ink',
    tileCls: 'bg-gold',
    iconCls: 'text-gold-foreground',
  },
];

export const DEFAULT_BACKEND_ID = 'opencode';

export const AGENT_BACKEND_BY_ID = Object.fromEntries(
  AGENT_BACKENDS.map((backend) => [backend.id, backend]),
) as Record<string, BackendUiMeta>;

export const DEFAULT_AGENT_STATE = Object.fromEntries(
  AGENT_BACKENDS.map((backend) => [
    backend.id,
    {
      enabled: backend.defaultEnabled,
      cli_path: backend.defaultCli,
      status: 'unknown',
    },
  ]),
);

export function getBackendUiMeta(id: string): BackendUiMeta {
  return (
    AGENT_BACKEND_BY_ID[id] || {
      id,
      label: id.replace(/_/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase()),
      defaultCli: id,
      defaultEnabled: false,
      settingsRoute: `/settings/backends/${id}`,
      Icon: Bot,
      initials: id.slice(0, 2).toUpperCase(),
      blockCls: 'border-border bg-surface-2 text-foreground',
      glyphCls: 'text-muted',
      tileCls: 'bg-surface-2',
      iconCls: 'text-muted',
    }
  );
}
