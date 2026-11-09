// The approved elevation and edge system (owner, 2026-11-08; docs/plans/elevation-system.md):
// shadows say only how high a surface sits (0 flat, 1 raised, 2 floating, 3 overlay); no opaque grey
// border on a resting card, input, soft button, or floating surface; state is fill, border, and ring,
// never a shadow or a glow; a glow is kept for what emits light; Light shadows take a brand tint, and
// the composer wears a gradient hairline and sweeps while an Agent works. Rules and tokens:
// kit/elevation/elevation.css.
//
// Each frame draws a shipped frame unchanged, inside <ElevationProposal>, so it differs from the
// frame it pairs with (named in its title) only by the system. As each part ships, these frames
// become `shipped` or are deleted in favour of the frame they pair with. Left: the two system
// sheets, the selected-Bot pairs (other fix #12) and the Light hover pairs (other fix #1).
import { frames, type FrameSpec } from '../../frame';
import { ElevationProposal } from '../../kit/elevation/ElevationProposal';
import { ElevationEdgeSheet } from '../../kit/elevation/ElevationEdgeSheet';
import { ElevationSheet } from '../../kit/elevation/ElevationSheet';
import { approved, pick } from '../../kit/elevation/pairs';
import agentsPage from '../agents/agents-page.design';
import sidebarBots from '../sidebar/bots.design';
import overlays from './overlays.design';
import { systemFixtures } from './fixtures';

const shipped = pick;

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

  ...reviewFrames,
]);

export default designFrames;
