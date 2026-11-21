// Proposed 2026-11-21: the Desktop App's own choice of path, 「自动」 or 「仅 Cloudflare 隧道」
// (kit/desktop-connection-choice.tsx). One choice for this computer, for every instance it opens.
//
// A (`menu-*`, recommended): a 「连接方式」 submenu in the app's own menus, next to 「切换实例」.
//   The app's other per-computer switches already live there (登录时启动, 通知, 接收预览版更新).
//   The shell owns it end to end: no page reads or changes it.
// B (`page-*`, the alternative): a 「连接方式」 section on the app's instances page.
//
// 「自动」: the direct path when the instance offers one and it works, otherwise the Cloudflare
// tunnel. 「仅 Cloudflare 隧道」: no direct path for any instance. A change applies to windows opened
// from then on; pages already open are not reloaded.
//
// The strings are the Devices page's shipped ones, read by key. When the Devices page's
// 「这台设备」 group reaches the Desktop App (with the page's `direct` capability), it shows and
// changes this same choice.
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { InstanceSwitcherView } from '@/desktop/InstanceSwitcherView';
import { frames, type FrameSpec } from '../../frame';
import { AccountPage, ConnectionChoiceMenu, ConnectionChoiceSection, type ConnectionChoice } from '../../kit/desktop-connection-choice';
import { workbenchFixtures } from '../workbench/fixtures';
import { copyFor, switcherViewProps } from './instances-fixtures';

type Lang = 'zh' | 'en';
const fixtures = (lang: Lang) => workbenchFixtures({}, { copy: copyFor(lang === 'en' ? en : zh) });

const menu = (name: string, title: string, system: 'macos' | 'windows', choice: ConnectionChoice, lang: Lang = 'zh'): FrameSpec => ({
  name, title, width: 620, height: 520, status: 'proposed', route: '/', fixtures: fixtures(lang),
  render: () => <ConnectionChoiceMenu choice={choice} system={system} />,
});

const page = (name: string, title: string, choice: ConnectionChoice): FrameSpec => {
  const switcher = switcherViewProps({ state: 'instances', lang: 'zh', connection: 'signed-in', updateAvailable: false });
  if (!switcher) throw new Error('no switcher fixture');
  return {
    name, title, width: 1200, height: 1040, status: 'proposed', themes: ['light'], route: '/', fixtures: fixtures('zh'),
    render: () => <AccountPage><InstanceSwitcherView {...switcher} /><ConnectionChoiceSection choice={choice} /></AccountPage>,
  };
};

const designFrames = frames([
  menu('menu-automatic', 'A · 托盘菜单（macOS）：「连接方式」子菜单，「自动」已选（默认）', 'macos', 'automatic'),
  menu('menu-tunnel', 'A · 托盘菜单（macOS）：选了「仅 Cloudflare 隧道」', 'macos', 'tunnel'),
  menu('menu-windows', 'A · 托盘菜单（Windows）：同样的两项', 'windows', 'automatic'),
  menu('menu-automatic-en', 'A · Tray menu (macOS), English', 'macos', 'automatic', 'en'),
  page('page-automatic', 'B（备选）· 实例页底部的「连接方式」一节，「自动」已选', 'automatic'),
  page('page-tunnel', 'B（备选）· 选了「仅 Cloudflare 隧道」', 'tunnel'),
]);

export default designFrames;
