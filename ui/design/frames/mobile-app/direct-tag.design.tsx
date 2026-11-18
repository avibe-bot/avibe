// Proposed 2026-11-18: a small 「直连」 tag beside the instance name while this page's live stream runs
// through the instance's direct ingress, as that stream's own handshake said (`via` on `/api/events`'
// `connected` frame). Any other way in shows nothing: no vendor name and no counts in the header.
// The details live in Settings · Remote access (settings/remote-access/direct-*).
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { Workbench } from '@/components/Workbench';
import { frames, type FrameSpec } from '../../frame';
import { IOS_APP_CAPABILITIES, IPHONE_17_PRO } from '../../shell/fixtures';
import type { DesignStreamEvent } from '../../shell/network';
import { agentsApi, mainRunningFixtures } from '../../shell/teamFixtures';
import { copyFor } from '../desktop/instances-fixtures';
import { homeFactsApi, phoneShell, workbenchFixtures } from '../workbench/fixtures';
import { SheetLanguage } from '../../kit/mobile-instance-sheet';

/** The stream's own handshake, saying how it came in. */
const handshake = (via: 'direct' | 'other'): DesignStreamEvent => ({ after: 0, event: 'connected', raw: true, data: { sub_id: 1, via } });

function frame(name: string, title: string, lang: 'zh' | 'en', via: 'direct' | 'other'): FrameSpec {
  return {
    name,
    title,
    ...IPHONE_17_PRO,
    status: 'proposed',
    themes: ['light', 'dark'],
    route: '/',
    wait: 300,
    fixtures: workbenchFixtures({
      ...agentsApi,
      ...homeFactsApi({ model: true, push: false, computerUse: true, chatApps: true, paired: true }),
      '/api/session': { remote: true, authenticated: true, email: 'max@example.com' },
    }, {
      ...mainRunningFixtures,
      nativeShell: { ...IOS_APP_CAPABILITIES, instancePresentation: 1 },
      nativeInstanceName: 'test-app',
      stream: [handshake(via)],
      copy: copyFor(lang === 'en' ? en : zh),
    }),
    render: () => <SheetLanguage lang={lang}>{phoneShell('/', <Workbench />)()}</SheetLanguage>,
  };
}

const designFrames = frames([
  frame('direct-zh', '本页连接经直连入口：实例名旁一个「直连」小标签', 'zh', 'direct'),
  frame('other-zh', '经其他方式（Cloudflare、局域网、本机）：什么都不显示', 'zh', 'other'),
  frame('direct-en', 'Through the direct ingress: a small "Direct" tag', 'en', 'direct'),
]);

export default designFrames;
