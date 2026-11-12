// Setup · model providers: the second screen of `/setup`, the shipped Wizard reached through the
// intro's 立即开始. Providers at the top feed the model gateway, which every assistant below shares.
import type { MigrationItem, Source } from '@/components/settings/models/types';
import { Wizard } from '@/components/Wizard';

import { frames, type FrameSpec } from '../../frame';
import { UNANSWERED } from '../../shell/network';
import { anthropicKey, setupFixtures, type SetupSpec } from './fixtures';

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

// ── A key setup just saved, and what its free check found ────────────────

/** An Anthropic key the wizard just saved: stored without verification, so nothing has
 *  listed its models yet. The card checks it on arrival. */
const savedKey: Source = {
  ...anthropicKey,
  state: { status: 'standby', retry_at: null, detail_key: null },
  verification_pending: 'vp_anthropic',
  credential_generation: 'gen_anthropic',
  models: [],
};
const check = `/api/models/sources/${savedKey.id}/refresh`;
const answer = (status: number, body: object) => new Response(
  JSON.stringify({ ok: status < 400, contract_version: 23, ...body }),
  { status, headers: { 'content-type': 'application/json' } },
);
/** The provider listed the key's models. */
const listed = { [check]: answer(200, { source: { ...anthropicKey, state: savedKey.state, verification_pending: 'vp_anthropic' }, removed_hops: [], interrupted: [] }) };
/** The provider refused the key itself. */
const refused = { [check]: answer(502, { error: 'discovery_failed', detail: 'modelHub.errors.authentication_failed', upstream_status: 401 }) };
/** The key works, but its new list drops a model Vibey routes to. */
const narrowed = {
  [check]: answer(409, {
    error: 'source_in_route_chain',
    detail: 'modelHub.errors.source_in_route_chain',
    would_remove_hops: [{ backend: 'vibey', menu_model: 'claude-opus-5', source_id: savedKey.id, model_id: 'claude-opus-5', position: 1 }],
    would_interrupt: [{ backend: 'vibey', model_id: 'claude-opus-5', agents: ['vibey'] }],
  }),
};
const keyCard = `.setup-provider-card[data-provider="${savedKey.vendor}"]`;
const saved = (api: Record<string, unknown>): SetupSpec => ({ sources: [savedKey], api });
/** A key that already supplies its list: the narrower list waits on the person. */
const supplying = (api: Record<string, unknown>): SetupSpec => ({ sources: [{ ...savedKey, models: anthropicKey.models }], api });

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
  screen('empty-phone', '设置向导 · 模型供应商 · 还没有供应商（手机，可稍后再配）', { sources: [] }, { width: 390, height: 844 }),
  screen('key-checking', '设置向导 · 模型供应商 · 刚保存的 Key 正在检查', saved({ [check]: UNANSWERED })),
  screen('key-connected', '设置向导 · 模型供应商 · Key 已连接，列出模型数', saved(listed)),
  screen('key-rejected', '设置向导 · 模型供应商 · Key 被拒绝，继续旁提示没有供应商通过', saved(refused)),
  screen('key-list-changed', '设置向导 · 模型供应商 · Key 可用但模型列表变少', supplying(narrowed)),
  screen('key-dialog', '设置向导 · 模型供应商 · 点被拒绝的卡片：换 Key 或移除', saved(refused), {
    click: [...toProviders, keyCard],
    hover: undefined,
  }),
  screen('key-remove-confirm', '设置向导 · 模型供应商 · 移除前确认', saved(refused), {
    click: [...toProviders, keyCard, 'role=button[name="{t:onboarding.providers.editRemove}"]'],
    hover: undefined,
  }),
  screen('key-list-changed-dialog', '设置向导 · 模型供应商 · 模型列表变少：说明并更新列表', supplying(narrowed), {
    click: [...toProviders, keyCard],
    hover: undefined,
  }),
  screen('key-rejected-phone', '设置向导 · 模型供应商 · Key 被拒绝（手机）', saved(refused), { width: 390, height: 844 }),
]);

export default designFrames;
