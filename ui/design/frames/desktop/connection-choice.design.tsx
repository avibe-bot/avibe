// Approved 2026-11-21 (Product): this computer's choice of path to an instance, in the Desktop
// App's own menus (kit/desktop-connection-choice.tsx): a 「连接方式」 submenu right after
// 「切换实例」, in the tray menu and the macOS application menu.
//
// The choice is per account and instance, as in the iPhone App: the submenu shows and sets it for
// the instance on show, and is greyed while this computer's own instance is on show.
// - 「自动」: the direct path when the instance offers one and it works, otherwise the Cloudflare
//   tunnel.
// - 「仅 Cloudflare 隧道」: no direct path for that instance.
// - 「仅直连和中继」 (Product, 2026-11-24, option A; proposed frames below): once a direct link is
//   up, never the tunnel. Connecting, and connecting again after a drop, goes through the tunnel
//   once. It is offered only for an instance that could be reached on a path that is not the
//   tunnel; for any other the item is absent.
//
// Between 「自动」 and 「仅 Cloudflare 隧道」 a change reloads nothing. Choosing 「仅直连和中继」,
// or leaving it, opens the instance again where it was. The strings are the Devices page's
// shipped ones, read by key, and that page's 「这台设备」 group shows and changes the same choice.
// The Desktop App sets the choice back to 「自动」 by itself only from 「仅直连和中继」 (the
// instance is reached on the tunnel and on no direct path); never from 「仅 Cloudflare 隧道」: it
// cannot reach on a direct path an instance it cannot reach on the tunnel.
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { frames, type FrameSpec } from '../../frame';
import { ConnectionChoiceMenu, type ConnectionChoice } from '../../kit/desktop-connection-choice';
import { workbenchFixtures } from '../workbench/fixtures';
import { copyFor } from './instances-fixtures';

type Lang = 'zh' | 'en';
const fixtures = (lang: Lang) => workbenchFixtures({}, { copy: copyFor(lang === 'en' ? en : zh) });

type Options = { lang?: Lang; direct?: boolean; status?: FrameSpec['status'] };

const menu = (name: string, title: string, system: 'macos' | 'windows', choice: ConnectionChoice | null, { lang = 'zh', direct = true, status = 'approved' }: Options = {}): FrameSpec => ({
  name, title, width: 900, height: 560, status, route: '/', fixtures: fixtures(lang),
  render: () => <ConnectionChoiceMenu choice={choice} system={system} direct={direct} />,
});

// The three-choice submenu, and the frames it changes, are proposed until Product signs this PR.
const proposed: Options = { status: 'proposed' };

const designFrames = frames([
  menu('menu-automatic', '托盘菜单（macOS）：「连接方式」子菜单，当前实例选的是「自动」（默认），三项', 'macos', 'automatic', proposed),
  menu('menu-tunnel', '托盘菜单（macOS）：当前实例选了「仅 Cloudflare 隧道」', 'macos', 'tunnel', proposed),
  menu('menu-direct', '托盘菜单（macOS）：当前实例选了「仅直连和中继」', 'macos', 'direct', proposed),
  menu('menu-local', '托盘菜单（macOS）：显示的是本机实例，「连接方式」置灰', 'macos', null),
  menu('menu-unroutable', '托盘菜单（macOS）：当前实例走不了直连（自定义域名），没有第三项', 'macos', 'automatic', { ...proposed, direct: false }),
  menu('menu-windows', '托盘菜单（Windows）：同样的三项', 'windows', 'automatic', proposed),
  menu('menu-direct-windows', '托盘菜单（Windows）：当前实例选了「仅直连和中继」', 'windows', 'direct', proposed),
  menu('menu-automatic-en', 'Tray menu (macOS), English', 'macos', 'automatic', { ...proposed, lang: 'en' }),
  menu('menu-direct-en', 'Tray menu (macOS), English: "Direct and relay only" chosen', 'macos', 'direct', { ...proposed, lang: 'en' }),
]);

export default designFrames;
