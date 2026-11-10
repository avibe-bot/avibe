// Setup · model providers: the second screen of `/setup`, the shipped Wizard reached through the
// intro's 立即开始. Providers at the top feed the model gateway, which every assistant below shares.
import type { MigrationItem } from '@/components/settings/models/types';
import { Wizard } from '@/components/Wizard';

import { frames, type FrameSpec } from '../../frame';
import { setupFixtures, type SetupSpec } from './fixtures';

const route = '/setup';
const wizard = () => <Wizard />;
/** The journey to this screen; its handoff plays on a timer, which `wait` lets settle. */
export const toProviders = ['role=button[name="{t:onboarding.welcome.getStarted}"]'];
/** Where the pointer rests once the journey is clicked through, so the last click leaves no hover behind. */
const rest = '[data-setup-screen-root="providers"] .onboarding-heading h1';

const screen = (name: string, title: string, spec: SetupSpec = {}, extra: Partial<FrameSpec> = {}): FrameSpec => ({
  name,
  title,
  width: 1440,
  height: 900,
  status: 'shipped',
  route,
  fixtures: setupFixtures(spec),
  click: toProviders,
  wait: 1000,
  hover: rest,
  render: wizard,
  ...extra,
});

/** What the scan finds on a machine where Claude Code and Codex were already signed in. */
const found: MigrationItem[] = [
  {
    id: 'mig_claude_key', backend: 'claude', kind: 'api_key', masked_detail: 'sk-ant…9c2E', proposed_action: 'import',
    selected: true, notes_key: null, vendor: 'anthropic', display_name: 'Anthropic', masked_credential: 'sk-ant…9c2E', source_paths: ['~/.claude/settings.json'], may_hold_api_key: true,
  },
  {
    id: 'mig_codex_key', backend: 'codex', kind: 'api_key', masked_detail: 'sk-proj…7Fq2', proposed_action: 'import',
    selected: true, notes_key: null, vendor: 'openai', display_name: 'OpenAI', masked_credential: 'sk-proj…7Fq2', source_paths: ['~/.codex/auth.json'], may_hold_api_key: true,
  },
];

const designFrames = frames([
  screen('default', '设置向导 · 模型供应商 · 已添加 OpenAI 与 Anthropic'),
  screen('empty', '设置向导 · 模型供应商 · 还没有供应商', { sources: [] }),
  screen('detected', '设置向导 · 模型供应商 · 发现本机已有的 Key', { sources: [], migration: found }),
  screen('add-more', '设置向导 · 模型供应商 · 添加更多', { sources: [] }, {
    click: [...toProviders, '.setup-provider-card[data-state="add"]'],
    hover: undefined,
  }),
  screen('add-api-key', '设置向导 · 模型供应商 · 添加 API Key', { sources: [] }, {
    click: [...toProviders, '.setup-provider-card[data-state="add"]', '.setup-add-method:nth-child(2)'],
    hover: undefined,
  }),
  screen('gateway-failed', '设置向导 · 模型供应商 · 模型网关未能启动', { health: 'down' }),
  screen('phone', '设置向导 · 模型供应商 · 手机（一屏放下，操作栏固定在底部）', {}, { width: 390, height: 844 }),
]);

export default designFrames;
