import { frames } from '../../frame';
import { AccountPage } from '../../kit/mobile-app';
import { bilingual, personal, smallCodeFixture } from './fixtures';

const designFrames = frames([
  ...bilingual('default', '账户 · 当前账号与实例', () => <AccountPage instance={personal} />),
  ...bilingual('long-email', '账户 · 完整长邮箱', () => <AccountPage instance={personal} />).map(frame => ({ ...frame, fixtures: { ...frame.fixtures, api: { ...frame.fixtures?.api, '/api/session': { remote: true, authenticated: true, email: smallCodeFixture.email } } } })),
  ...bilingual('no-instance', '账户 · 尚未选择实例', () => <AccountPage />),
]);

export default designFrames;
