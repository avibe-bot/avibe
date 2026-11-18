import { frames } from '../../frame';
import { EmptyInstancesPage } from '../../kit/mobile-app';
import { bilingual } from './fixtures';
import { EmptyInstancesScreen } from '@/mobile/MobileOnboarding';

const noop = () => {};

const designFrames = frames([
  ...bilingual('default', '没有实例 · 桌面版与 CLI', () => <EmptyInstancesPage />),
  ...bilingual('cli', 'CLI · 在电脑安装并配对', () => <EmptyInstancesPage cli />),
  // A Google, GitHub or Apple sign-in just created this account and it has no instances
  // (Product, 2026-11-18): one line and sign-out above the usual ways to add an instance.
  ...bilingual('new-account', '没有实例 · 第三方登录刚新建的账号：一行说明 + 退出登录', () => (
    <EmptyInstancesScreen
      cli={false}
      busy={false}
      locked={false}
      errorKey={null}
      newAccount
      onLogout={noop}
      onBack={noop}
      onAccount={noop}
      onRefresh={noop}
      onShowCli={noop}
      onOpenGuide={noop}
      onOpenDownload={noop}
    />
  ), [390, 1000]).map(frame => ({ ...frame, status: 'proposed' as const })),
]);

export default designFrames;
