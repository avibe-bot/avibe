// Proposed 2026-11-18: one quiet line under the instance name in the phone app's header, saying which
// path this page's NEW connections try first (`direct.status().preferred`), never where an existing
// connection runs. It shows only with the shell's `direct` capability; without it the header is as
// shipped (mobile-app/switcher/b-home). A closure count since the App started is appended once it is
// above zero. Contract: docs/plans/remote-direct-ios.md section 10.
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { Workbench } from '@/components/Workbench';
import { frames, type FrameSpec } from '../../frame';
import { IOS_APP_CAPABILITIES, IPHONE_17_PRO, type WorkbenchFixtures } from '../../shell/fixtures';
import { agentsApi, mainRunningFixtures } from '../../shell/teamFixtures';
import { copyFor } from '../desktop/instances-fixtures';
import { homeFactsApi, phoneShell, workbenchFixtures } from '../workbench/fixtures';
import { SheetLanguage } from '../../kit/mobile-instance-sheet';

/** The App that names the instance (`instancePresentation`), as it ships today. */
const SHELL = { ...IOS_APP_CAPABILITIES, instancePresentation: 1 };

type Direct = WorkbenchFixtures['nativeDirectStatus'];

function fixtures(lang: 'zh' | 'en', direct: Direct) {
  return workbenchFixtures({
    ...agentsApi,
    ...homeFactsApi({ model: true, push: false, computerUse: true, chatApps: true, paired: true }),
    '/api/session': { remote: true, authenticated: true, email: 'max@example.com' },
  }, {
    ...mainRunningFixtures,
    nativeShell: direct ? { ...SHELL, direct: 1 } : SHELL,
    nativeInstanceName: 'test-app',
    ...(direct ? { nativeDirectStatus: direct } : {}),
    copy: copyFor(lang === 'en' ? en : zh),
  });
}

function both(name: string, title: string, direct: Direct): FrameSpec[] {
  return (['zh', 'en'] as const).map((lang) => ({
    name: `${name}-${lang}`,
    title: `${title} · ${lang === 'zh' ? '中文' : 'English'}`,
    ...IPHONE_17_PRO,
    status: 'proposed',
    themes: ['light', 'dark'],
    route: '/',
    fixtures: fixtures(lang, direct),
    render: () => <SheetLanguage lang={lang}>{phoneShell('/', <Workbench />)()}</SheetLanguage>,
  }));
}

const designFrames = frames([
  ...both('absent', '没有 direct 能力（浏览器、桌面、未注册的 App）：顶栏与现在完全一样，不显示、不读取', undefined),
  ...both('direct', '新连接优先走直连', { preferred: 'direct', sequence: 2, closure: 0 }),
  ...both('tunnel', '新连接优先走 Cloudflare', { preferred: 'tunnel', sequence: 1, closure: 0 }),
  ...both('closures', '直连路径中断过：在同一行后面加次数（App 启动以来）', { preferred: 'tunnel', sequence: 5, closure: 2 }),
]);

export default designFrames;
