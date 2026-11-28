import { EmptyInstancesScreen } from '@/mobile/MobileOnboarding';
import { frames } from '../../frame';
import { bilingual, noop } from './fixtures';

const emptyStep = (props: Partial<Parameters<typeof EmptyInstancesScreen>[0]> = {}) => () => (
  <EmptyInstancesScreen
    cli={false}
    busy={false}
    locked={false}
    errorKey={null}
    onBack={noop}
    onAccount={noop}
    onRefresh={noop}
    onShowCli={noop}
    onOpenGuide={noop}
    onOpenDownload={noop}
    {...props}
  />
);

const designFrames = frames([
  ...bilingual('default', '没有实例 · 桌面版与 CLI', emptyStep()),
  ...bilingual('cli', 'CLI · 在电脑安装并配对', emptyStep({ cli: true })),
  // A Google, GitHub or Apple sign-in just created this account and it has no instances
  // (Product, 2026-11-18): one small grey line after the install hint, ending in a sign-out link.
  ...bilingual('new-account', '没有实例 · 第三方登录刚新建的账号：安装说明后一行灰字 + 退出登录链接', emptyStep({ newAccount: true, onLogout: noop }), [390, 1000]),
]);

export default designFrames;
