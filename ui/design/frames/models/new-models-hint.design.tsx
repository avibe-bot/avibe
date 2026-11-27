// New models you can add, offered only where the user already chooses a model (owner, 2026-11-27).
//
// A model is offered when the user's own Source lists it, it is that vendor's own chat model, and the
// Vibey menu does not hold it yet. Vibey's Session model menu then ends its list with one quiet row:
// one model is named in the row with 「添加」; more are counted, and the row opens to name them. 「添加」
// puts the model in the menu and picks it, as the menu's 添加模型 already does; the row shrinks to what is
// left, and goes with the last. × (「不再提示」) ends it for those models; so does showing in three
// opened menus without an add. Settings › 模型 › add lists the same models first, and keeps offering
// them after the row is gone. Nothing else marks them: no badge, no dot, no notification, so whoever
// never opens the menu sees nothing.
//
// Model names here are fixtures; the row's own copy names none.
import { AddModelsDraft, ModelMenuDraft, type DraftCandidate, type DraftModel, type NewModelsHint } from '../../kit/new-models-hint';
import { frames, type FrameSpec } from '../../frame';

const gptEfforts = ['low', 'medium', 'high', 'xhigh'];
const claudeEfforts = ['low', 'medium', 'high', 'max'];

// The menu as the chat frames have it: three models, GPT-6 Astra picked at 高.
const astra: DraftModel = { id: 'gpt-6-astra', label: 'GPT-6 Astra', efforts: gptEfforts };
const sol: DraftModel = { id: 'gpt-5.6-sol', label: 'GPT-5.6 Sol', efforts: gptEfforts };
const opus5: DraftModel = { id: 'claude-opus-5', label: 'Claude Opus 5', efforts: claudeEfforts };
const menu = [astra, sol, opus5];

// What a vendor release brought: one model from each of two Sources.
const opus55: DraftModel = { id: 'claude-opus-5-5', label: 'Claude Opus 5.5', efforts: claudeEfforts };
const terra: DraftModel = { id: 'gpt-6.1-terra', label: 'GPT-6.1 Terra', efforts: gptEfforts };

type MenuState = { models: DraftModel[]; current: string; hint: NewModelsHint };
const states: Record<string, MenuState & { title: string; hover?: string }> = {
  one: { title: '一个新模型：行里直接写名字和「添加」', models: menu, current: astra.id, hint: { kind: 'one', model: opus55 } },
  two: { title: '两个新模型：「有 2 个新模型可以添加」', models: menu, current: astra.id, hint: { kind: 'many', models: [opus55, terra], open: false } },
  'two-open': { title: '两个新模型 · 点开：逐个写名字，各带「添加」', models: menu, current: astra.id, hint: { kind: 'many', models: [opus55, terra], open: true } },
  'two-added': {
    title: '点了 Claude Opus 5.5 的「添加」：它进了菜单并被选中，提示行缩成剩下的一个',
    models: [...menu, opus55],
    current: opus55.id,
    hint: { kind: 'one', model: terra },
  },
  'all-added': { title: '最后一个也添加了：提示行消失', models: [...menu, opus55, terra], current: terra.id, hint: { kind: 'none' } },
  close: {
    title: '关闭：× 悬停写「不再提示」',
    models: menu,
    current: astra.id,
    hint: { kind: 'many', models: [opus55, terra], open: false },
    hover: '[data-hint-close]',
  },
  closed: { title: '点了 × 之后：菜单回到原样（模型仍可在 设置 › 模型 添加）', models: menu, current: astra.id, hint: { kind: 'none' } },
};

const menuFrames: FrameSpec[] = Object.entries(states).flatMap(([name, state]) => {
  const render = () => <ModelMenuDraft models={state.models} current={state.current} effort="high" hint={state.hint} />;
  return [
    { name, title: `Vibey 模型菜单 · ${state.title}`, width: 1000, height: 760, status: 'proposed' as const, hover: state.hover, render },
    // A phone has no hover: × is a plain control there, so the close state is the row as drawn.
    ...(state.hover ? [] : [{ name: `${name}-phone`, title: `Vibey 模型菜单 · ${state.title} · 手机`, width: 390, height: 844, status: 'proposed' as const, render }]),
  ];
});

// Settings › 模型 › 添加模型 for Vibey: the new models first, with who lists them; the Sources' folds
// below hold the rest, so a model is offered once.
const fresh: DraftCandidate[] = [
  { model: opus55, suppliers: ['Claude API'] },
  { model: terra, suppliers: ['ChatGPT Pro'] },
];
const providers = [
  {
    name: 'ChatGPT Pro',
    rows: [{ id: 'gpt-5.6-terra', label: 'GPT-5.6 Terra', efforts: [] }, { id: 'gpt-5.5', label: 'GPT-5.5', efforts: [] }].map((model) => ({ model, suppliers: ['ChatGPT Pro'] })),
  },
  {
    name: 'Claude API',
    rows: [{ id: 'claude-sonnet-5', label: 'Claude Sonnet 5', efforts: [] }, { id: 'claude-haiku-5', label: 'Claude Haiku 5', efforts: [] }].map((model) => ({ model, suppliers: ['Claude API'] })),
  },
];
const settingsRender = () => <AddModelsDraft fresh={fresh} providers={providers} />;

const designFrames = frames([
  ...menuFrames,
  { name: 'settings-add', title: '设置 › 模型 › 添加 Vibey 模型：新模型排在最前', width: 1000, height: 820, status: 'proposed', render: settingsRender },
  { name: 'settings-add-phone', title: '设置 › 模型 › 添加 Vibey 模型：新模型排在最前 · 手机', width: 390, height: 844, status: 'proposed', render: settingsRender },
]);

export default designFrames;
