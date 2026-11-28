import { AccountDataPage, AccountPage } from '@/mobile/MobileOnboarding';
import { frames } from '../../frame';
import { bilingual, longEmail, noop, personal, signedInSession } from './fixtures';

const accountScreen = (props: Partial<Parameters<typeof AccountPage>[0]> = {}) => () => (
  <AccountPage
    session={signedInSession([personal])}
    busy={false}
    locked={false}
    errorKey={null}
    licenses={false}
    showCurrent
    onBack={noop}
    onSwitch={noop}
    onLogout={noop}
    onLicenses={noop}
    onAccountData={noop}
    {...props}
  />
);

const designFrames = frames([
  ...bilingual('default', '账户 · 当前账号与实例', accountScreen()),
  ...bilingual('long-email', '账户 · 完整长邮箱', accountScreen({ session: signedInSession([personal], personal.id, longEmail) })),
  ...bilingual('no-instance', '账户 · 尚未选择实例', accountScreen({ session: signedInSession([], null) })),
  // The full-page Account and data (from an instance's avatar); the sheet's is switcher/account-data.
  ...bilingual('data', '账号与数据 · 整页：隐私政策，最底部一行红字「删除账号」', () => <AccountDataPage locked={false} onBack={noop} onPrivacy={noop} onDelete={noop} />),
]);

export default designFrames;
