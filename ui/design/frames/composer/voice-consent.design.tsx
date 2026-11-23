// Asking before voice input goes to a third-party AI service (App Store guideline 5.1.2(i)). The
// first press of any voice control on this Avibe (holding the composer, the mic button or ⌥Z, the
// tab bar's main Bot, a Bot's avatar, a Show Page's dictation) asks once per account, before the
// microphone opens; existing users are asked once on their next press. The body says where speech
// goes; "Learn more" opens the small print. Allow: the next press records as it does today. Don't
// allow: voice stays off, typing is unchanged, and the next press asks again (owner decision,
// 2026-11-17: a mis-tap is recoverable). The desktop mini window says where to turn it on. Settings › General holds
// the answer (on asks the question; off is at once).
// Every part is shipped.
import type { ReactNode } from 'react';

import { SettingsGeneralPage } from '@/components/settings/SettingsGeneralPage';
import { VoiceConsentDialog } from '@/components/voice/VoiceConsent';

import { frames } from '../../frame';
import { DesktopScreen } from '../../shell/DesktopScreen';
import { botConversationApi, demoProjects, demoSessions, inboxRow, mainBot, message, type WorkbenchFixtures } from '../../shell/fixtures';
import { PhoneConversation } from '../../shell/PhoneScreen';
import { agentsApi } from '../../shell/teamFixtures';
import { settingsFixtures, settingsPage } from '../settings/fixtures';

// An everyday exchange with the main Bot behind the question, so nothing in the background reads
// as a warning (owner direction, 2026-11-16).
const reply = '排好了：上午坐高铁到西湖边散步，中午在湖滨吃饭，下午去龙井村喝茶，傍晚回程。要我顺便查一下高铁票吗？';
const everyday = [
  message('ses_main', 'e1', 'user', '下周六想带家人去杭州玩一天，帮我排个不赶的行程。', 14),
  message('ses_main', 'e2', 'agent', reply, 12),
];
const status = (consent: 'granted' | 'declined' | null) => ({ '/api/asr/status': { available: true, max_file_bytes: null, consent }, '/api/cloud/token': { ok: false } });
const everydayFixtures = (consent: 'granted' | 'declined' | null = null): WorkbenchFixtures => ({
  projects: demoProjects,
  sessionsByProject: demoSessions,
  bots: [mainBot],
  inboxSessions: [inboxRow('ses_main', null, '', reply, 12)],
  api: { ...agentsApi, ...botConversationApi(mainBot, [mainBot], everyday), ...status(consent) },
});

const route = '/chat/ses_main';
const phone: [number, number] = [390, 844];
const desktop: [number, number] = [1440, 900];
const noop = () => {};

const frame = (name: string, title: string, size: [number, number], render: () => ReactNode, fixtures: WorkbenchFixtures = everydayFixtures(), at = route) => ({
  name,
  title,
  width: size[0],
  height: size[1],
  status: 'shipped' as const,
  fixtures,
  route: at,
  render,
});

const ask = (screen: 'phone' | 'desktop', expanded = false) => () => (
  <>
    {screen === 'phone' ? <PhoneConversation /> : <DesktopScreen />}
    <VoiceConsentDialog open defaultExpanded={expanded} onAllow={noop} onDeny={noop} onClose={noop} />
  </>
);


const settings = settingsPage('general', <SettingsGeneralPage />);

const designFrames = frames([
  frame('b-details-phone', '1 · 第一次按住说话（老用户：升级后第一次）：麦克风还没打开，先问一次', phone, ask('phone')),
  frame('b-details-phone-open', '1 · 点「了解详情」：小字写明经 avibe.bot、服务商与地区、不保存、在哪里更改、隐私政策', phone, ask('phone', true)),
  frame('b-details-desktop', '1 · 同上，桌面', desktop, ask('desktop')),
  frame('d-settings-off', '3 · 设置 › 常规：语音输入（按账号、按这个 Avibe）。打开时先问一次；关闭立即生效', [1440, 1280], settings, settingsFixtures(status('declined')), '/settings/general'),
  frame('d-settings-on-phone', '3 · 同上，已允许，手机', [390, 1760], settings, settingsFixtures(status('granted')), '/settings/general'),
]);

export default designFrames;
