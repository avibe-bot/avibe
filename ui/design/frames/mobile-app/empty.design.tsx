import { frames } from '../../frame';
import { EmptyInstancesPage } from '../../kit/mobile-app';
import { bilingual } from './fixtures';

const designFrames = frames([
  ...bilingual('default', '没有实例 · 桌面版与 CLI', () => <EmptyInstancesPage />),
  ...bilingual('cli', 'CLI · 在电脑安装并配对', () => <EmptyInstancesPage cli />),
]);

export default designFrames;
