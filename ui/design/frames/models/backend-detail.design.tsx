// Settings · Backends · one backend: the shipped Claude Code, Codex, OpenCode, and Vibey pages —
// runtime and lifecycle chip, 模型管理方式 (gateway or direct), sign-in, and 测试连通性 — plus what a
// click opens on them: the lifecycle chip's popover in each state, and a sign-in in progress.
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

// The lifecycle chip opens its popover on click; the chip is named by its state.
const chip = (state: 'statusUpdateAvailable' | 'statusReady' | 'statusDisabled' | 'statusError') =>
  `role=button[name="{t:backendLifecycle.${state}}"]`;

// 立即升级 starts an install job that is still running when the frame is drawn.
const upgrading = backendsFixtures({
  api: {
    '/api/agent/claude/install': { ok: true, status: 'running', job_id: 'job_claude_upgrade', poll_timeout_seconds: 600 },
    '/api/agent/claude/install/job_claude_upgrade': { ok: true, status: 'running', job_id: 'job_claude_upgrade' },
  },
});

// OpenCode's CLI is gone from PATH after a reinstall.
const opencodeMissing = backendsFixtures({
  runtimes: { opencode: { installed: false, status: 'missing', resolved_path: null, current_version: null, reason: 'not_found', process_status: 'stopped' } },
});

// 使用 Claude 登录 on Claude Code: the authorization link, waiting for the code the browser shows.
const claudeSignIn = backendsFixtures({
  api: {
    '/api/backend/claude/auth/oauth/start': { ok: true, flow_id: 'flow_claude', backend: 'claude', state: 'awaiting_code', callback_kind: 'code', url: 'https://claude.ai/oauth/authorize?code=true&client_id=9d1c250a&response_type=code&scope=user%3Ainference' },
    '/api/backend/claude/auth/oauth/status/flow_claude': { ok: true, flow_id: 'flow_claude', backend: 'claude', state: 'awaiting_code', callback_kind: 'code', url: 'https://claude.ai/oauth/authorize?code=true&client_id=9d1c250a&response_type=code&scope=user%3Ainference' },
  },
});

// Codex run directly on a ChatGPT sign-in, then 重新登录: a device code to enter on OpenAI's page.
const codexDevice = { ok: true, flow_id: 'flow_codex', backend: 'codex', state: 'awaiting_code', callback_kind: 'device', url: 'https://auth.openai.com/codex/device', device_code: 'GVHQ-LMXR' };
const codexSignIn = backendsFixtures({
  api: {
    '/api/backend/codex/connection': { ok: true, backend: 'codex', installed: true, cli_status: 'ok', enabled: true, auth: 'subscription', application: 'applied', ready: true, entry_eligible: true, supply_mode: 'direct' },
    '/api/models/agents': { ok: true, contract_version: 23, agents: [{ backend: 'codex', cli_present: true, mode: 'direct', menu_kind: 'fixed' }] },
    '/api/backend/codex/auth': { ok: true, auth_mode: 'oauth', active_auth_mode: 'oauth', has_api_key: false, api_key_length: 0, api_key_masked: null, base_url: null, has_chatgpt_tokens: true, chatgpt_account: 'lin@chainbot.io', credentials_store: 'file', file_store_active: true },
    '/api/backend/codex/auth/oauth/start': codexDevice,
    '/api/backend/codex/auth/oauth/status/flow_codex': codexDevice,
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
  {
    name: 'lifecycle-update',
    title: '后端 · 生命周期 · 可升级',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/settings/backends/claude',
    fixtures: backendsFixtures(),
    click: [chip('statusUpdateAvailable')],
    render: claude,
  },
  {
    name: 'lifecycle-update-phone',
    title: '后端 · 生命周期 · 可升级 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/settings/backends/claude',
    fixtures: backendsFixtures(),
    click: [chip('statusUpdateAvailable')],
    render: claude,
  },
  {
    name: 'lifecycle-upgrading',
    title: '后端 · 生命周期 · 升级中',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/settings/backends/claude',
    fixtures: upgrading,
    click: [chip('statusUpdateAvailable'), 'role=button[name="{t:backendLifecycle.upgradeNow}"]'],
    render: claude,
  },
  {
    name: 'lifecycle-ready',
    title: '后端 · 生命周期 · 已是最新',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/settings/backends/codex',
    fixtures: backendsFixtures(),
    click: [chip('statusReady')],
    render: codex,
  },
  {
    name: 'lifecycle-disabled',
    title: '后端 · 生命周期 · 已停用',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/settings/backends/codex',
    fixtures: backendsFixtures({ enabled: { codex: false } }),
    click: [chip('statusDisabled')],
    render: codex,
  },
  {
    name: 'lifecycle-missing',
    title: '后端 · 生命周期 · CLI 未安装',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/settings/backends/opencode',
    fixtures: opencodeMissing,
    click: [chip('statusError')],
    render: settingsPage('backends/opencode', <SettingsOpencodeProviderPage />),
  },
  {
    name: 'claude-sign-in',
    title: '后端 · Claude Code · 登录进行中（授权码）',
    width: 1440,
    height: 1400,
    status: 'shipped',
    route: '/settings/backends/claude',
    fixtures: claudeSignIn,
    click: ['role=button[name="{t:settings.backends.claudeSignInButton}"]'],
    render: claude,
  },
  {
    name: 'codex-sign-in',
    title: '后端 · Codex · 登录进行中（设备码）',
    width: 1440,
    height: 1400,
    status: 'shipped',
    route: '/settings/backends/codex',
    fixtures: codexSignIn,
    click: ['role=button[name="{t:settings.backends.oauthReauthenticate}"]'],
    render: codex,
  },
]);

export default designFrames;
