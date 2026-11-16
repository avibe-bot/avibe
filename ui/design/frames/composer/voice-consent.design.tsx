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
import { GeneralWithVoiceInput, VoiceConsentDialog, VoiceOffNote, type VoiceSupplier } from '../../kit/voice-consent';
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

const ask = (supplier: VoiceSupplier, screen: 'phone' | 'desktop') => () => (
  <>
    {screen === 'phone' ? <PhoneConversation /> : <DesktopScreen />}
    <VoiceConsentDialog supplier={supplier} />
  </>
);

const off = (screen: 'phone' | 'desktop') => () => {
  const note = <ComposerNoteLayer><VoiceOffNote /></ComposerNoteLayer>;
  return screen === 'phone' ? <PhoneConversation overlay={note} /> : <DesktopScreen overlay={note} />;
};

const settings = (enabled: boolean) => settingsPage('general', <GeneralWithVoiceInput enabled={enabled} />);

const designFrames = frames([
  frame('a-ask-phone', '1 · 第一次按住说话（老用户：升级后第一次）：麦克风还没打开，先问一次；手机上是底部弹层', phone, ask('personal', 'phone')),
  frame('a-ask-desktop', '1 · 同上，桌面：对话框；点「允许」后下一次按住照常录音', desktop, ask('personal', 'desktop')),
  frame('a-ask-org-phone', '1b · 组织的 Avibe：服务由组织管理员选定，不写具体厂商', phone, ask('organization', 'phone')),
  frame('b-off-phone', '2 · 选了「不允许」后再按住（或点麦克风）：不录音，提示怎么开启；打字照常', phone, off('phone')),
  frame('b-off-desktop', '2 · 同上，桌面', desktop, off('desktop')),
  frame('c-settings-off', '3 · 设置 › 常规：语音输入（按账号、按这个 Avibe）。打开时再弹一次第 1 步的说明；关闭立即生效', desktop, settings(false), { route: '/settings/general', fixtures: settingsFixtures() }),
  frame('c-settings-on-phone', '3 · 同上，已允许，手机', phone, settings(true), { route: '/settings/general', fixtures: settingsFixtures() }),
]);

export default designFrames;
