// Approved 2026-11-21 (Product): this computer's choice of path to an instance, 「自动」 or
// 「仅 Cloudflare 隧道」, in the Desktop App's own menus (kit/desktop-connection-choice.tsx): a
// 「连接方式」 submenu right after 「切换实例」, in the tray menu and the macOS application menu.
//
// The choice is per account and instance, as in the iPhone App: the submenu shows and sets it for
// the instance on show, and is greyed while this computer's own instance is on show.
// 「自动」: the direct path when the instance offers one and it works, otherwise the Cloudflare
// tunnel. 「仅 Cloudflare 隧道」: no direct path for that instance. A change applies to windows
// opened from then on; pages already open are not reloaded.
//
// The strings are the Devices page's shipped ones, read by key. When the Devices page's
// 「这台设备」 group reaches the Desktop App (with the page's `direct` capability), it shows and
// changes this same choice. Unlike the iPhone App, the Desktop App does not set the choice back to
// 「自动」 by itself: it cannot reach on a direct path an instance it cannot reach on the tunnel.
import { desktopEn as en, desktopZh as zh } from '@/i18n/desktop';
import { frames, type FrameSpec } from '../../frame';
import { ConnectionChoiceMenu, type ConnectionChoice } from '../../kit/desktop-connection-choice';
import { workbenchFixtures } from '../workbench/fixtures';
import { copyFor } from './instances-fixtures';

type Lang = 'zh' | 'en';
const fixtures = (lang: Lang) => workbenchFixtures({}, { copy: copyFor(lang === 'en' ? en : zh) });

const menu = (name: string, title: string, system: 'macos' | 'windows', choice: ConnectionChoice | null, lang: Lang = 'zh'): FrameSpec => ({
  name, title, width: 900, height: 560, status: 'approved', route: '/', fixtures: fixtures(lang),
  render: () => <ConnectionChoiceMenu choice={choice} system={system} />,
});

const designFrames = frames([
  menu('menu-automatic', '托盘菜单（macOS）：「连接方式」子菜单，当前实例选的是「自动」（默认）', 'macos', 'automatic'),
  menu('menu-tunnel', '托盘菜单（macOS）：当前实例选了「仅 Cloudflare 隧道」', 'macos', 'tunnel'),
  menu('menu-local', '托盘菜单（macOS）：显示的是本机实例，「连接方式」置灰', 'macos', null),
  menu('menu-windows', '托盘菜单（Windows）：同样的两项', 'windows', 'automatic'),
  menu('menu-automatic-en', 'Tray menu (macOS), English', 'macos', 'automatic', 'en'),
]);

export default designFrames;
