// The elevation and edge system (owner, 2026-11-08; docs/plans/elevation-system.md): shadows say
// only how high a surface sits (0 flat, 1 raised, 2 floating, 3 overlay); no opaque grey border on a
// resting card, input, soft button, or floating surface; state is fill, border, and ring, never a
// shadow or a glow; a glow is kept for what emits light, in the theme's own accent; Light shadows take
// a brand tint, and the composer wears a gradient hairline and sweeps while an Agent works. The two
// sheets draw the system with the shipped tokens and primitives.
import { frames } from '../../frame';
import { ElevationEdgeSheet } from './elevation-edge-sheet';
import { ElevationSheet } from './elevation-sheet';
import { systemFixtures } from './fixtures';

const designFrames = frames([
  {
    name: 'scale',
    title: '层级、状态与光',
    width: 1180,
    height: 860,
    status: 'shipped',
    fixtures: systemFixtures(),
    render: () => <ElevationSheet />,
  },
  {
    name: 'edges',
    title: '边与面、输入框、浅色里的光',
    width: 1180,
    height: 860,
    status: 'shipped',
    fixtures: systemFixtures(),
    render: () => <ElevationEdgeSheet />,
  },
]);

export default designFrames;
