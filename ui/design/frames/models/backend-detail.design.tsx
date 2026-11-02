// Settings · Backends · one backend: the shipped Claude Code, Codex, OpenCode, and Vibey pages —
// runtime and lifecycle chip, 模型管理方式 (gateway or direct), sign-in, and 测试连通性.
import { SettingsBackendPage } from '@/components/settings/SettingsBackendPage';
import { SettingsClaudeProviderPage } from '@/components/settings/SettingsClaudeProviderPage';
import { SettingsCodexProviderPage } from '@/components/settings/SettingsCodexProviderPage';
import { SettingsOpencodeProviderPage } from '@/components/settings/SettingsOpencodeProviderPage';

import { frames } from '../../frame';
import { settingsPage } from '../settings/fixtures';
import { backendsFixtures } from './backends.fixtures';

const claude = settingsPage('backends/claude', <SettingsClaudeProviderPage />);
const codex = settingsPage('backends/codex', <SettingsCodexProviderPage />);

// Codex run directly, with an API key and a relay Base URL instead of the gateway.
const codexDirect = backendsFixtures({
  api: {
    '/api/backend/codex/connection': { ok: true, backend: 'codex', installed: true, cli_status: 'ok', enabled: true, auth: 'api_key', application: 'applied', ready: true, entry_eligible: true, supply_mode: 'direct' },
    '/api/models/agents': { ok: true, contract_version: 23, agents: [{ backend: 'codex', cli_present: true, mode: 'direct', menu_kind: 'fixed' }] },
  },
});

const designFrames = frames([
  {
    name: 'claude',
    title: '后端 · Claude Code · 直连、本机登录',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/settings/backends/claude',
    fixtures: backendsFixtures(),
    render: claude,
  },
  {
    name: 'claude-phone',
    title: '后端 · Claude Code · 手机',
    width: 390,
    height: 1000,
    status: 'shipped',
    route: '/settings/backends/claude',
    fixtures: backendsFixtures(),
    render: claude,
  },
  {
    name: 'codex',
    title: '后端 · Codex · 模型网关',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/settings/backends/codex',
    fixtures: backendsFixtures(),
    render: codex,
  },
  {
    name: 'codex-direct',
    title: '后端 · Codex · 直连、API Key',
    width: 1440,
    height: 1000,
    status: 'shipped',
    route: '/settings/backends/codex',
    fixtures: codexDirect,
    render: codex,
  },
  {
    name: 'opencode',
    title: '后端 · OpenCode · 服务商',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/settings/backends/opencode',
    fixtures: backendsFixtures(),
    render: settingsPage('backends/opencode', <SettingsOpencodeProviderPage />),
  },
  {
    name: 'vibey',
    title: '后端 · Vibey（内置）',
    width: 1440,
    height: 600,
    status: 'shipped',
    route: '/settings/backends/vibey',
    fixtures: backendsFixtures(),
    render: settingsPage('backends/vibey', <SettingsBackendPage backend="vibey" />),
  },
]);

export default designFrames;
