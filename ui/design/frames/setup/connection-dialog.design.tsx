// Setup · connection dialog: the shipped BackendConnectionDialog an assistant opens when it signs
// in on its own rather than through the model gateway (a direct assistant on the AI assistants
// screen). Claude Code and Codex choose subscription or API key; OpenCode chooses a provider first.
import { Wizard } from '@/components/Wizard';

import { frames, type FrameSpec } from '../../frame';
import { toAssistants } from './assistants.design';
import { connectionApi, setupFixtures } from './fixtures';

const route = '/setup';
const wizard = () => <Wizard />;

/** Every CLI assistant is ticked, signs in on its own, and has no connection yet. */
const fixtures = setupFixtures({
  assistants: {
    claude: { enabled: true, mode: 'direct', auth: 'none' },
    codex: { enabled: true, mode: 'direct', auth: 'none' },
    opencode: { enabled: true, mode: 'direct', auth: 'none' },
  },
  api: connectionApi,
});

const card = (backend: string, key: string) => `.onboarding-assistant[data-backend="${backend}"] >> role=button[name="{t:${key}}"]`;
const addSubscription = (backend: string) => card(backend, 'onboarding.connection.addSubscription');
const addKey = (backend: string) => card(backend, 'onboarding.connection.addKey');
const inDialog = (selector: string) => `role=dialog >> ${selector}`;

const dialog = (name: string, title: string, click: string[], extra: Partial<FrameSpec> = {}): FrameSpec => ({
  name,
  title,
  width: 1440,
  height: 900,
  status: 'shipped',
  route,
  fixtures,
  click: [...toAssistants, ...click],
  wait: 1000,
  // The pointer rests on the title, so the last click leaves no hover behind.
  hover: '[role="dialog"] h2',
  render: wizard,
  ...extra,
});

const designFrames = frames([
  dialog('claude-subscription', '连接弹窗 · Claude Code · 使用 Claude 账号登录', [addSubscription('claude')]),
  dialog('claude-code', '连接弹窗 · Claude Code · 粘贴授权码', [addSubscription('claude'), inDialog('role=button[name="{t:onboarding.connection.claudeSignIn}"]')]),
  dialog('claude-api-key', '连接弹窗 · Claude Code · API Key', [addKey('claude')]),
  dialog('claude-auth-token', '连接弹窗 · Claude Code · Auth Token', [addKey('claude'), inDialog('role=radio[name="{t:onboarding.connection.authTokenLabel}"]')]),
  dialog('codex-subscription', '连接弹窗 · Codex · 使用 ChatGPT 登录', [addSubscription('codex')]),
  dialog('codex-device', '连接弹窗 · Codex · 设备授权码', [addSubscription('codex'), inDialog('role=button[name="{t:onboarding.connection.codexSignIn}"]')]),
  dialog('codex-api-key', '连接弹窗 · Codex · OpenAI API Key', [addKey('codex')]),
  dialog('opencode-subscription', '连接弹窗 · OpenCode · 选择订阅服务商', [addSubscription('opencode')]),
  dialog('opencode-device', '连接弹窗 · OpenCode · GitHub Copilot 设备授权', [
    addSubscription('opencode'),
    inDialog('.connection-provider:has-text("GitHub Copilot")'),
    inDialog('role=button[name="{t:settings.backends.opencodeProviderSignIn}"]'),
  ]),
  dialog('opencode-callback', '连接弹窗 · OpenCode · ChatGPT 浏览器授权回调', [
    addSubscription('opencode'),
    inDialog('.connection-provider:has-text("OpenAI")'),
    inDialog('role=button[name="{t:settings.backends.opencodeProviderSignIn}"]'),
  ]),
  dialog('opencode-api-key', '连接弹窗 · OpenCode · 选择 API Key 服务商', [addKey('opencode')]),
  dialog('opencode-provider-key', '连接弹窗 · OpenCode · DeepSeek API Key', [addKey('opencode'), inDialog('.connection-provider:has-text("DeepSeek")')]),
  dialog('phone', '连接弹窗 · Claude Code · 手机', [addSubscription('claude')], { width: 390, height: 844 }),
]);

export default designFrames;
