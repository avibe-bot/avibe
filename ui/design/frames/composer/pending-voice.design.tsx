// Speech kept on the device until its words land (owner rule, 2026-11-05: the user's speech must
// never be lost). A take that could not be transcribed, or whose words have not been sent or put
// in the draft yet, stays as a small item above the composer of the conversation it was spoken
// into, through a reload or a crash, until the reader acts. One with no text yet retries on its
// own when the page is back online or in front, and on request; one transcribed waits to be sent
// (a hold) or put in the draft (a tap or ⌥Z dictation). Every part is shipped.
import type { ReactNode } from 'react';

import type { PendingVoiceItemProps } from '@/components/voice/PendingVoiceTakes';

import { frames } from '../../frame';
import { DesktopScreen } from '../../shell/DesktopScreen';
import type { WorkbenchFixtures } from '../../shell/fixtures';
import { KeptVoiceTakes } from '../../shell/HeldComposer';
import { PhoneConversation } from '../../shell/PhoneScreen';
import { csoFixtures, said } from './fixtures';

const phone = (
  name: string,
  title: string,
  items: PendingVoiceItemProps[],
  fixtures: WorkbenchFixtures = csoFixtures({ hint: false }),
) => ({
  name,
  title,
  width: 390,
  height: 844,
  status: 'shipped' as const,
  fixtures,
  route: '/chat/ses_cso',
  render: (): ReactNode => <PhoneConversation overlay={<KeptVoiceTakes items={items} />} />,
});

const desktop = (name: string, title: string, items: PendingVoiceItemProps[]) => ({
  name,
  title,
  width: 1440,
  height: 900,
  status: 'shipped' as const,
  fixtures: csoFixtures({ hint: false }),
  route: '/chat/ses_cso',
  render: (): ReactNode => <DesktopScreen overlay={<KeptVoiceTakes items={items} />} />,
});

const minutes = (seconds: number) => [{ sequence: 0, final: true, durationMs: seconds * 1000 }];
const label = '说给首席安全官';
const waiting: PendingVoiceItemProps = { take: { status: 'pending', label, intent: 'send', segments: minutes(142) } };
const retrying: PendingVoiceItemProps = { ...waiting, retrying: true };
const failed: PendingVoiceItemProps = { ...waiting, failed: true };
const ready: PendingVoiceItemProps = { take: { status: 'transcribed', text: said.final, label, intent: 'send', segments: minutes(142) }, onSend: () => {} };
const dictated: PendingVoiceItemProps = { take: { status: 'transcribed', text: '把令牌刷新的修复方案整理一下，周一前给我。', label: null, intent: 'insert', segments: minutes(9) } };
const lost: PendingVoiceItemProps = { take: { status: 'pending', label, intent: 'send', segments: [{ sequence: 0, final: true, durationMs: 0, empty: true }] } };

const designFrames = frames([
  phone('a-waiting', '1 · 断网时说的一段：录音留在这台设备上（重新打开也在），网络恢复或回到前台会自动重试', [waiting]),
  phone('b-retrying', '2 · 正在重试：用保存的录音整段重新识别', [retrying]),
  phone('c-failed', '3 · 这次也没成功：录音还在，可以再试，或者不要了', [failed]),
  phone('d-ready', '4 · 识别出来了，还没发：按住说的话可以发送，也可以放进输入框改一改', [ready]),
  phone('e-dictated', '4b · 点按/⌥Z 听写的一段：只放进输入框，不直接发送', [dictated]),
  phone('e2-truncated', '4c · 页面在保存最后一段时崩了：只识别保存下来的部分，并明确说结尾没存下来', [{ ...ready, take: { ...ready.take, truncated: true } }]),
  phone('f-several', '5 · 有好几段（最多保留 3 段，再多会挤掉最早的那段）', [ready, failed, waiting]),
  phone('g-lost', '× · 这段录音没能存下来（页面在第一分钟内就崩了）：只能不要了', [lost]),
  desktop('h-waiting-desktop', '1 · 桌面：断网时说的一段，留在输入框上方', [waiting]),
  desktop('i-ready-desktop', '4 · 桌面：识别出来了，发送或放进输入框', [ready, dictated]),
]);

export default designFrames;
