// Setup · AI assistants: the third screen of `/setup`, the shipped Wizard reached the way a person
// reaches it, through the intro's 立即开始 and the providers' 继续. Each assistant's card shows its
// lifecycle, its switch, and the model its built-in Agent uses; Vibey is built in and always on, and
// a fresh install leaves the CLI assistants' switches off until the person ticks them. The CLI
// assistants start folded under 更多助手 beside Vibey; a frame about them unfolds them first.
import { Wizard } from '@/components/Wizard';

import { frames, type FrameSpec } from '../../frame';
import { candidatesApi, setupFixtures, type SetupSpec } from './fixtures';

const route = '/setup';
const wizard = () => <Wizard />;
/** The journey to this screen. Each screen change plays a timed handoff, which `wait` lets settle. */
export const toAssistants = ['role=button[name="{t:onboarding.welcome.getStarted}"]', 'role=button[name="{t:onboarding.providers.actionContinue}"]'];
const settle = 1000;
/** The journey on, unfolding 更多助手. */
export const toMoreAssistants = [...toAssistants, 'role=button[name="{t:onboarding.setup.moreAssistantsShow}"]'];
/** Where the pointer rests once the journey is clicked through, so the last click leaves no hover behind. */
const rest = '[data-setup-screen-root="assistants"] .onboarding-heading h1';

const screen = (name: string, title: string, spec: SetupSpec = {}, extra: Partial<FrameSpec> = {}): FrameSpec => ({
  name,
  title,
  width: 1440,
  height: 900,
  status: 'shipped',
  route,
  fixtures: setupFixtures(spec),
  click: toAssistants,
  wait: settle,
  hover: rest,
  render: wizard,
  ...extra,
});

/** All three CLI assistants ticked by hand. */
const ticked = { claude: { enabled: true }, codex: { enabled: true }, opencode: { enabled: true } } as const;

/** Claude Code has an update, Codex is switched off, OpenCode is not installed yet. */
const mixed: SetupSpec = {
  assistants: { claude: { enabled: true, update: true }, codex: { enabled: false }, opencode: { enabled: true, cli: 'missing' } },
};

const designFrames = frames([
  screen('default', '设置向导 · AI 助手 · 新安装：Vibey 就绪，其他助手未勾选'),
  screen('more-shown', '设置向导 · AI 助手 · 展开更多助手', {}, { click: toMoreAssistants }),
  screen('more-shown-phone', '设置向导 · AI 助手 · 展开更多助手（手机）', {}, { click: toMoreAssistants, width: 390, height: 844 }),
  screen('folded-enabled', '设置向导 · AI 助手 · 折叠时标出已启用的助手', { assistants: { claude: { enabled: true } } }),
  screen('ticked', '设置向导 · AI 助手 · 三个助手都已勾选，全部就绪', { assistants: ticked }, { click: toMoreAssistants }),
  screen('vibey-no-model', '设置向导 · AI 助手 · Vibey 还没有模型，卡内选择', {
    assistants: { ...ticked, vibey: { model: null } },
    api: candidatesApi,
  }),
  screen('vibey-no-model-phone', '设置向导 · AI 助手 · Vibey 还没有模型，卡内选择（手机）', {
    assistants: { ...ticked, vibey: { model: null } },
    api: candidatesApi,
  }, { width: 390, height: 844 }),
  screen('nothing-ready', '设置向导 · AI 助手 · Vibey 没有模型、助手都未勾选', {
    assistants: { vibey: { model: null } },
    api: candidatesApi,
  }),
  screen('model-picker', '设置向导 · AI 助手 · 点 Claude Code 的默认模型：选择模型', { assistants: ticked, api: candidatesApi }, {
    click: [...toMoreAssistants, '.onboarding-assistant[data-backend="claude"] .onboarding-model-choice'],
    hover: undefined,
  }),
  screen('mixed', '设置向导 · AI 助手 · 可升级、已停用、未安装', mixed, { click: toMoreAssistants }),
  screen('update-details', '设置向导 · AI 助手 · Claude Code 的版本详情', mixed, {
    click: [...toMoreAssistants, '.onboarding-assistant[data-backend="claude"] >> role=button[name="{t:backendLifecycle.statusUpdateAvailable}"]'],
  }),
  screen('card-hover', '设置向导 · AI 助手 · 鼠标移上 Codex 卡片', {}, { click: toMoreAssistants, hover: '.onboarding-assistant[data-backend="codex"]' }),
  screen('opencode-permission', '设置向导 · AI 助手 · OpenCode 请求允许工具调用', { permissionRequired: true }, { click: toMoreAssistants }),
  screen('permission-refused', '设置向导 · AI 助手 · 未能允许工具调用', {
    permissionRequired: true,
    api: { '/api/opencode/setup-permission': { ok: false, message: '无法写入 /Users/demo/.config/opencode/opencode.json：权限不足', config_path: '/Users/demo/.config/opencode/opencode.json' } },
  }, {
    click: [...toMoreAssistants, '.onboarding-assistant[data-backend="opencode"] >> role=button[name="{t:onboarding.setup.allowToolCalls}"]'],
  }),
  screen('direct', '设置向导 · AI 助手 · 自带登录：订阅与 API Key 已连接', {
    assistants: {
      claude: { enabled: true, mode: 'direct', auth: 'subscription' },
      codex: { enabled: true, mode: 'direct', auth: 'api_key' },
      opencode: { enabled: true, mode: 'direct', auth: 'none' },
    },
  }, { click: toMoreAssistants }),
  screen('short', '设置向导 · AI 助手 · 矮窗口 1366×768', {}, { width: 1366, height: 768 }),
  screen('large', '设置向导 · AI 助手 · 大屏 1920×1080', {}, { width: 1920, height: 1080 }),
  screen('phone', '设置向导 · AI 助手 · 手机（卡片随内容高，操作栏固定在底部）', {}, { width: 390, height: 844 }),
]);

export default designFrames;
