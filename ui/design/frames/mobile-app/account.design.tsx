import { frames } from '../../frame';
import { AccountPage } from '../../kit/mobile-app';
import { bilingual, personal } from './fixtures';

const designFrames = frames([
  ...bilingual('default', '账户 · 当前账号与实例', () => <AccountPage instance={personal} />),
  ...bilingual('no-instance', '账户 · 尚未选择实例', () => <AccountPage />),
]);

export default designFrames;
