// The motion tokens in src/index.css, each shown moving (docs/plans/web-ui-motion.md §4).
import { frames } from '../../frame';
import { TokenSheet } from '../../kit/motion/TokenSheet';

const designFrames = frames([
  { name: 'sheet', title: '动效令牌：时长、曲线、弹簧、加载', width: 900, height: 960, status: 'approved', render: () => <TokenSheet /> },
]);

export default designFrames;
