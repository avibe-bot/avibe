// A small tag beside the instance name while this page's live stream runs through the instance's
// direct ingress, as that stream's own handshake said (`via` on `/api/events`' `connected` frame),
// naming the App's kind of path (`direct.status()`, `direct` version 3): 「直连」 only for an IPv4 or
// IPv6 address, 「中继」 through the relay (Product, 2026-11-19: never 「直连」 for a relayed path).
// Any other way in, a browser, or a path the App does not name, shows nothing: no vendor name and no
// counts in the header. The details live in Settings · Remote access (settings/remote-access/direct-*).
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

function frame(
  name: string,
  title: string,
  lang: 'zh' | 'en',
  via: 'direct' | 'other',
  path: 'relay' | 'ipv4' | 'ipv6' = 'ipv6',
): FrameSpec {
  return {
    name,
    title,
    ...IPHONE_17_PRO,
    status: 'shipped',
    themes: ['light', 'dark'],
    route: '/',
    wait: 300,
    fixtures: workbenchFixtures({
      ...agentsApi,
      ...homeFactsApi({ model: true, push: false, computerUse: true, chatApps: true, paired: true }),
      '/api/session': { remote: true, authenticated: true, email: 'max@example.com' },
    }, {
      ...mainRunningFixtures,
      nativeShell: { ...IOS_APP_CAPABILITIES, instancePresentation: 1, direct: 3 },
      nativeDirectStatus: { preferred: 'direct', sequence: 3, closure: 0, path },
      nativeInstanceName: 'test-app',
      stream: [handshake(via)],
      copy: copyFor(lang === 'en' ? en : zh),
    }),
    render: () => <SheetLanguage lang={lang}>{phoneShell('/', <Workbench />)()}</SheetLanguage>,
  };
}

const designFrames = frames([
  frame('direct-zh', '本页经直连入口、App 走 IPv4/IPv6 地址：实例名旁一个「直连」小标签', 'zh', 'direct'),
  frame('relay-zh', '本页经直连入口、App 走中继：「中继」，从不称为直连', 'zh', 'direct', 'relay'),
  frame('other-zh', '经其他方式（Cloudflare、局域网、本机）：什么都不显示', 'zh', 'other'),
  frame('direct-en', 'Through the direct ingress on an address: a small "Direct" tag', 'en', 'direct'),
  frame('relay-en', 'Through the direct ingress over the relay: "Relay"', 'en', 'direct', 'relay'),
]);

export default designFrames;
