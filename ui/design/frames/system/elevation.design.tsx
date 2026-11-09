// The approved elevation and edge system (owner, 2026-11-08; docs/plans/elevation-system.md):
// shadows say only how high a surface sits (0 flat, 1 raised, 2 floating, 3 overlay); no opaque grey
// border on a resting card, input, soft button, or floating surface; state is fill, border, and ring,
// never a shadow or a glow; a glow is kept for what emits light; Light shadows take a brand tint, and
// the composer wears a gradient hairline and sweeps while an Agent works. Rules and tokens:
// kit/elevation/elevation.css.
//
// Each frame draws a shipped frame unchanged, inside <ElevationProposal>, so it differs from the
// frame it pairs with (named in its title) only by the system. As each part ships, these frames
// become `shipped` or are deleted in favour of the frame they pair with.
import { BackendLifecycleChip } from '@/components/settings/BackendLifecycleChip';

import { frames, type FrameSpec } from '../../frame';
import { ElevationProposal } from '../../kit/elevation/ElevationProposal';
import { ElevationEdgeSheet } from '../../kit/elevation/ElevationEdgeSheet';
import { ElevationSheet } from '../../kit/elevation/ElevationSheet';
import { approved, pick } from '../../kit/elevation/pairs';
import agentsPage from '../agents/agents-page.design';
import dock from '../apps/dock.design';
import home from '../home/home.design';
import settingsGeneral from '../settings/general.design';
import inboxPage from '../inbox/inbox-page.design';
import workbenchTab from '../mobile/workbench-tab.design';
import sidebarBots from '../sidebar/bots.design';
import workbenchSidebar from '../sidebar/workbench-sidebar.design';
import { RUNTIMES } from '../models/backends.fixtures';
import overlays from './overlays.design';
import { systemFixtures } from './fixtures';

const shipped = pick;

/** The shipped frame `<file>/<state>`, drawn under the approved system. */
const after = (name: string, file: string, specs: FrameSpec[], state: string): FrameSpec =>
  approved(shipped(file, specs, state), name, `${file}/${state}`);

// A toast over content: the shipped toast frame raises one over an empty page, where a see-through
// fill cannot show. The same trigger, with a page of transcript-like text under the toast's corner.
const filler = Array.from({ length: 14 }, (_, i) => i);
const toastOverContent = () => (
  <div className="relative h-full">
    <div className="flex flex-col gap-3 p-6 pt-16 pr-8 text-[13px] leading-relaxed text-foreground">
      {filler.map((i) => (
        <p key={i}>
          已经读完现有 Web UI 代码。收件箱、项目、新建会话和应用各自一个入口，聊天页和搜索页全屏，进入时隐藏底栏。项目页按最近活跃排序，长按可以置顶。
        </p>
      ))}
    </div>
    <div className="absolute right-6 top-4">
      <BackendLifecycleChip name="codex" enabled cliStatus="ok" cliPath="codex" detectedRuntime={RUNTIMES.codex} />
    </div>
  </div>
);
const toastFrame = {
  width: 800,
  height: 560,
  fixtures: systemFixtures({ '/api/backend/codex/runtime': RUNTIMES.codex, '/api/backend/codex/restart': { ok: true, message: '' } }),
  click: ['role=button[name="{t:backendLifecycle.statusReady}"]', 'role=button[name="{t:backendLifecycle.restart}"]'],
};

// Review pairs that need a state the shipped frames do not hold: a Bot selected in the strip, and a
// hover in Light. `before` is the shipped frame with that state added; the pair adds the system.
const variant = (spec: FrameSpec, name: string, title: string, extra: Partial<FrameSpec>): [FrameSpec, FrameSpec] => {
  const before: FrameSpec = { ...spec, ...extra, name: `${name}-before`, title: `现状 · ${title}`, status: 'shipped' };
  return [before, approved(before, name, `system/elevation/${name}-before`)];
};
const mainOpen = shipped('sidebar/bots', sidebarBots, 'main-open');
const reviewFrames: FrameSpec[] = [
  ...variant(mainOpen, 'bots-selected-main', '伙伴条 · 在歪比的对话里（歪比选中）', {}),
  ...variant(mainOpen, 'bots-selected-pm', '伙伴条 · 在产品经理的对话里（产品经理选中，首席安全官有未读）', { route: '/chat/ses_pm' }),
  ...variant(shipped('system/overlays', overlays, 'dialog'), 'hover-dialog-ghost', '悬停 · 对话框里的「取消」（ghost 按钮）', { hover: 'role=button[name="取消"]' }),
  ...variant(shipped('agents/agents-page', agentsPage, 'definitions'), 'hover-agents-row', '悬停 · Agents 列表行（docs-writer）', { hover: 'button:has-text("docs-writer")' }),
];

const designFrames = frames([
  {
    name: 'scale',
    title: '定稿 · 层级、状态与光',
    width: 1180,
    height: 860,
    status: 'approved',
    fixtures: systemFixtures(),
    render: () => (
      <ElevationProposal>
        <ElevationSheet />
      </ElevationProposal>
    ),
  },

  {
    name: 'edges',
    title: '定稿 · 边与面、输入框、浅色里的光',
    width: 1180,
    height: 860,
    status: 'approved',
    fixtures: systemFixtures(),
    render: () => (
      <ElevationProposal>
        <ElevationEdgeSheet />
      </ElevationProposal>
    ),
  },

  // Desktop pages.
  after('inbox-unread', 'inbox/inbox-page', inboxPage, 'unread'),
  after('sidebar-hover-card', 'sidebar/bots', sidebarBots, 'hover-card'),
  after('sidebar-selected', 'sidebar/workbench-sidebar', workbenchSidebar, 'skills-selected'),
  after('dock', 'apps/dock', dock, 'open'),
  after('home', 'home/home', home, 'b-partial-desktop'),
  after('settings-general', 'settings/general', settingsGeneral, 'default'),
  { name: 'toast-before', title: '现状 · 提示条盖在内容上', status: 'shipped', ...toastFrame, render: toastOverContent },
  {
    name: 'toast',
    title: '定稿 · 提示条盖在内容上（对照 system/elevation/toast-before）',
    status: 'approved',
    ...toastFrame,
    render: () => <ElevationProposal>{toastOverContent()}</ElevationProposal>,
  },

  // Phone.
  after('phone-inbox', 'inbox/inbox-page', inboxPage, 'phone'),
  after('phone-tabbar', 'mobile/workbench-tab', workbenchTab, 'team'),
  after('phone-tabbar-recording', 'mobile/workbench-tab', workbenchTab, 'hold-main-recording'),

  ...reviewFrames,
]);

export default designFrames;
