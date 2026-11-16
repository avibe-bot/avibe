// Asking before voice input goes to third-party AI (App Store guideline 5.1.2(i)). The first press
// of any voice control on this Avibe (holding the composer, the mic button or ⌥Z, the tab bar's main
// Bot, a Bot's avatar, the desktop mini window, a Show Page's dictation) asks once per account,
// before the microphone opens. Existing users are asked once on their next press. "Allow": the
// next press records as it does today. "Don't allow": voice stays off, typing is unchanged, and a
// press leaves a note that says how to turn it on. Settings › General holds the answer.
// The dialog, the note and the Settings row are proposals (kit/voice-consent.tsx); everything
// around them is shipped.
import type { ReactNode } from 'react';

import { frames } from '../../frame';
import { DesktopScreen } from '../../shell/DesktopScreen';
import { ComposerNoteLayer } from '../../shell/HeldComposer';
import { PhoneConversation } from '../../shell/PhoneScreen';
import { GeneralWithVoiceInput, VoiceConsentDialog, VoiceOffNote, type VoiceConsentVariant } from '../../kit/voice-consent';
import { settingsFixtures, settingsPage } from '../settings/fixtures';
import { csoFixtures } from './fixtures';

const route = '/chat/ses_cso';
const phone: [number, number] = [390, 844];
const desktop: [number, number] = [1440, 900];

const frame = (name: string, title: string, size: [number, number], render: () => ReactNode, extra: { route?: string; fixtures?: ReturnType<typeof csoFixtures> } = {}) => ({
  name,
  title,
  width: size[0],
  height: size[1],
  status: 'proposed' as const,
  fixtures: extra.fixtures ?? csoFixtures(),
  route: extra.route ?? route,
  render,
});

const ask = (variant: VoiceConsentVariant, screen: 'phone' | 'desktop', expanded = false) => () => (
  <>
    {screen === 'phone' ? <PhoneConversation /> : <DesktopScreen />}
    <VoiceConsentDialog variant={variant} expanded={expanded} />
  </>
);

const off = (screen: 'phone' | 'desktop') => () => {
  const note = <ComposerNoteLayer><VoiceOffNote /></ComposerNoteLayer>;
  return screen === 'phone' ? <PhoneConversation overlay={note} /> : <DesktopScreen overlay={note} />;
};

const settings = (enabled: boolean) => settingsPage('general', <GeneralWithVoiceInput enabled={enabled} />);

const designFrames = frames([
  frame('a-link-phone', 'A · 第一次按住说话（老用户：升级后第一次）：标题、一句话、「了解详情」打开隐私政策的语音一节', phone, ask('link', 'phone')),
  frame('a-link-desktop', 'A · 同上，桌面', desktop, ask('link', 'desktop')),
  frame('b-details-phone', 'B · 一句话（含上下文），「了解详情」在弹层里展开小字', phone, ask('details', 'phone')),
  frame('b-details-phone-open', 'B · 展开的小字：经 avibe.bot、服务商与地区、不保存、在哪里更改、隐私政策', phone, ask('details', 'phone', true)),
  frame('b-details-desktop', 'B · 同上，桌面', desktop, ask('details', 'desktop')),
  frame('c-off-phone', '2 · 选了「不允许」后再按住（或点麦克风）：不录音，提示怎么开启；打字照常', phone, off('phone')),
  frame('c-off-desktop', '2 · 同上，桌面', desktop, off('desktop')),
  frame('d-settings-off', '3 · 设置 › 常规：语音输入（按账号、按这个 Avibe）。打开时再问一次；关闭立即生效', desktop, settings(false), { route: '/settings/general', fixtures: settingsFixtures() }),
  frame('d-settings-on-phone', '3 · 同上，已允许，手机', phone, settings(true), { route: '/settings/general', fixtures: settingsFixtures() }),
]);

export default designFrames;
