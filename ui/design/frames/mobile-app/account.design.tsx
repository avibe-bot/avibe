import { frames } from '../../frame';
import { AccountPage } from '../../kit/mobile-app';
import { bilingual, personal, smallCodeFixture } from './fixtures';
import { AccountDataPage } from '@/mobile/MobileOnboarding';

const noop = () => {};

const designFrames = frames([
  ...bilingual('default', '账户 · 当前账号与实例', () => <AccountPage instance={personal} />),
  ...bilingual('long-email', '账户 · 完整长邮箱', () => <AccountPage instance={personal} />).map(frame => ({ ...frame, fixtures: { ...frame.fixtures, api: { ...frame.fixtures?.api, '/api/session': { remote: true, authenticated: true, email: smallCodeFixture.email } } } })),
  ...bilingual('no-instance', '账户 · 尚未选择实例', () => <AccountPage />),
  // The full-page Account and data (from an instance's avatar); the sheet's is switcher/account-data.
  ...bilingual('data', '账号与数据 · 整页：隐私政策，最底部一行灰字「删除账号」', () => <AccountDataPage locked={false} onBack={noop} onPrivacy={noop} onDelete={noop} />).map(frame => ({ ...frame, status: 'shipped' as const })),
]);

export default designFrames;
