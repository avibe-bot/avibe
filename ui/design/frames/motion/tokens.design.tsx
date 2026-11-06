// The motion tokens in src/index.css, each shown moving (docs/plans/web-ui-motion.md §4).
import { frames } from '../../frame';
import { TokenSheet } from '../../kit/motion/TokenSheet';

const designFrames = frames([
  {
    name: 'sheet',
    title: '动效令牌：时长、曲线、弹簧、页面、加载',
    width: 900,
    height: 1040,
    status: 'approved',
    // The shell's Bots provider reads the Bots for every frame; this sheet shows none.
    fixtures: { bots: [] },
    render: () => <TokenSheet />,
  },
]);

export default designFrames;
