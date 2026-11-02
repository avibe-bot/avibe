// Settings pages render inside the shipped Settings layout (its header and rail), routed the way
// App.tsx routes them, and read the Owner's config (`config.json`: a `GET /api/config` answer for
// an install with Slack, Discord, and Telegram connected; secrets appear only as `has_*` flags).
import type { ReactNode } from 'react';
import { Route, Routes } from 'react-router-dom';

import { SettingsLayout } from '@/components/settings/SettingsLayout';

import { DESIGN_NOW } from '../../shell/clock';
import { mainBot, type WorkbenchFixtures } from '../../shell/fixtures';
import { agentCatalog } from '../agents/fixtures';
import config from './config.json';

export const settingsConfig = config;

const at = (minutesAgo: number) => new Date(DESIGN_NOW - minutesAgo * 60_000).toISOString();

export const running = { state: 'running', pid: 48213, started_at: at(4 * 60 + 23) };

/** What every Settings page reads: the running service, the config, the version, a local session. */
export function settingsFixtures(api: Record<string, unknown> = {}, extra: WorkbenchFixtures = {}): WorkbenchFixtures {
  return {
    status: running,
    bots: [mainBot],
    ...extra,
    api: {
      '/api/config': config,
      '/api/version': { current: '3.2.4', latest: '3.2.4', has_update: false, error: null },
      '/api/session': { remote: false },
      ...api,
    },
  };
}

/** One Settings page at `/settings/<path>` inside the shipped layout. */
export const settingsPage = (path: string, element: ReactNode) => () => (
  <Routes>
    <Route path="/settings" element={<SettingsLayout />}>
      <Route path={path} element={element} />
    </Route>
  </Routes>
);

const routing = (agentName: string | null, extra: Record<string, unknown> = {}) => ({
  enabled: true,
  show_message_types: ['assistant'],
  custom_cwd: '',
  routing: { agent_name: agentName, model: null, reasoning_effort: null },
  require_mention: null,
  require_bind: null,
  ...extra,
});

/** Per-platform scope settings (`GET /api/settings?platform=…`): group and user routes plus the Agent catalog. */
export const scopeSettings = {
  slack: {
    channels: {
      C04AIBOTS: routing('vibey', { custom_cwd: '/Users/demo/dev/avibe-bot-backend', require_mention: true }),
      C01GENERAL: routing(null),
      C07RELEASE: routing('reviewer', { enabled: false }),
    },
    agent_catalog: agentCatalog,
  },
  discord: {
    channels: { C03DEVBOTS: routing('codex', { enabled: false }), C05AIHANDS: routing('claude') },
    guild_allowlist: ['G01AIHANDS'],
    agent_catalog: agentCatalog,
  },
  telegram: {
    channels: { '-1002233445566': routing('slack-triage') },
    threads: {},
    agent_catalog: agentCatalog,
  },
};

export const scopeSettingsApi = Object.fromEntries(
  Object.entries(scopeSettings).map(([platform, settings]) => [`/api/settings?platform=${platform}`, settings]),
);

/** The groups each platform reports (`POST /api/<platform>/channels`, `/api/telegram/chats`). */
export const platformChannelsApi = {
  '/api/slack/channels': {
    ok: true,
    channels: [
      { id: 'C04AIBOTS', name: 'ai-bots', is_private: false },
      { id: 'C01GENERAL', name: 'general', is_private: false },
      { id: 'C07RELEASE', name: 'release-notes', is_private: true },
    ],
  },
  '/api/discord/channels': {
    ok: true,
    channels: [
      { id: 'C05AIHANDS', name: 'ai-hands', type: 0 },
      { id: 'C03DEVBOTS', name: 'dev-bots', type: 0 },
    ],
  },
  '/api/telegram/chats': {
    ok: true,
    channels: [{ id: '-1002233445566', name: '产品讨论组', type: 'supergroup', supports_topics: false, last_seen_at: at(42) }],
  },
};

const user = (displayName: string, agentName: string | null, extra: Record<string, unknown> = {}) => ({
  display_name: displayName,
  is_admin: false,
  bound_at: at(60 * 24 * 12),
  ...routing(agentName),
  ...extra,
});

/** The users bound on each platform (`GET /api/users?platform=…`). */
export const usersApi = {
  '/api/users?platform=slack': {
    ok: true,
    users: {
      U04ALEXLIU: user('Alex Liu', 'vibey', { is_admin: true, custom_cwd: '/Users/demo/dev/avibe-bot-backend', show_message_types: ['assistant', 'toolcall'] }),
      B07DEVOPS: user('devops-bot', null, { enabled: false }),
    },
  },
  '/api/users?platform=discord': { ok: true, users: { '319204857761': user('Sara Chen', 'opencode') } },
  '/api/users?platform=telegram': { ok: true, users: { '58181121': user('Marc Lin', 'reviewer') } },
};

/** One active bind code with time left, and an older one already used. */
export const bindCodesApi = {
  '/api/bind-codes': {
    ok: true,
    bind_codes: [
      { code: 'VBE-7HT3-Q9PX', type: 'expiring', created_at: at(20), expires_at: new Date(DESIGN_NOW + 60 * 60_000 * 24).toISOString(), is_active: true, used_by: [] },
      { code: 'VBE-2KD8-M4RA', type: 'one_time', created_at: at(60 * 24 * 12), expires_at: null, is_active: false, used_by: ['U04ALEXLIU'] },
    ],
  },
};
