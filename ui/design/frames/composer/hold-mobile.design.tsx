// Hold the composer to talk, on a phone (owner direction, 2026-11-02), as on the center of the
// tab bar: hold 300 ms (moving 10 px first lets the page scroll), slide up 80 px to arm cancel
// (back under 60 px disarms), release to send to this conversation. It records only while the
// composer is empty and the keyboard is down; a tap raises the keyboard, and once it is up, or
// there is text, a long press is the system's (caret, selection, Paste).
import { frames } from '../../frame';
import { ComposerHoldLayer, SoftKeyboard, SystemEditMenu, type HoldNote, type HoldPhase } from '../../kit/ComposerHold';
import { PhoneConversation } from '../../shell/PhoneScreen';
import type { WorkbenchFixtures } from '../../shell/fixtures';
import { csoFixtures, csoSentMessages, said, saidLong, saidSession, sessionFixtures, staged, typed } from './fixtures';
import type { ReactNode } from 'react';

const cso = { bot: 'cso' };

const frame = (
  name: string,
  title: string,
  render: () => ReactNode,
  fixtures: WorkbenchFixtures = csoFixtures(),
  size: [number, number] = [390, 844],
  route = '/chat/ses_cso',
) => ({
  name,
  title,
  width: size[0],
  height: size[1],
  status: 'approved' as const,
  fixtures,
  route,
  render,
});

const hold = (phase: HoldPhase, extra: Partial<Parameters<typeof ComposerHoldLayer>[0]> = {}) => () => (
  <PhoneConversation overlay={<ComposerHoldLayer device="phone" to={cso} phase={phase} {...extra} />} />
);
const note = (which: HoldNote) => () => <PhoneConversation overlay={<ComposerHoldLayer device="phone" to={cso} note={which} />} />;

/** The conversation above the keyboard, the way the visual viewport shrinks when it rises. */
const withKeyboard = (overlay?: ReactNode) => () => (
  <div className="flex h-full flex-col bg-background">
    <div className="relative min-h-0 flex-1 transform-gpu">
      <PhoneConversation overlay={overlay} />
    </div>
    <SoftKeyboard />
  </div>
);

const small: [number, number] = [375, 667];

const designFrames = frames([
  frame('a-idle', '1 · 静止：输入框为空、键盘没弹起，占位提示「输入消息，或按住说话」', () => <PhoneConversation />),
  frame('b-pressed', '2 · 按下（未到 300ms：松开 = 点一下，弹出键盘）', hold('pressing')),
  frame('c-listening', '3 · 按满 300ms：开始录音，还没出字', hold('listening', { seconds: 1 })),
  frame('d-recording', '4 · 录音中：实时转写，上滑取消 · 松开发送', hold('recording', { text: said.text, stash: said.stash, seconds: 6 })),
  frame('d-recording-long', '4b · 说得很长：转写区滚动，停在最新一行，不限时长', hold('recording', { text: saidLong.text, stash: saidLong.stash, seconds: 48, long: true })),
  frame('e-armed', '5 · 上滑超过 80px：松开取消（滑回 60px 以下恢复）', hold('armed', { text: said.text, stash: said.stash, seconds: 7 })),
  frame('f-settling', '6 · 松开：整理最终文字（最长 20 秒）', hold('settling', { text: said.final, seconds: 8 })),
  frame('d-recording-attachment', '4c · 只放了附件、没有文字：照样按住录音，附件和语音一起发', hold('recording', { text: said.text, stash: said.stash, seconds: 6, attachment: staged })),
  frame('g-sent', '7 · 已发送：消息直接进了对话（伙伴对话不排队），首席安全官开始工作；占位写「首席安全官正在处理…」，照样可以按住说话', () => <PhoneConversation />, csoFixtures({ messages: csoSentMessages, running: true })),
  frame('h-cancelled', '× · 在取消区松开：没有发送', note('cancelled')),
  frame('i-too-short', '× · 太短（不到 0.5 秒）或没听到说话：没有发送', note('too-short')),
  frame('j-asking', '× · 系统询问麦克风权限（主屏 App 会反复问）：这一次不录音', note('asking')),
  frame('k-denied', '× · 麦克风被拒绝：说明去哪里打开，可以直接打字', note('denied')),
  frame('l-unavailable', '× · 语音服务暂时不可用：可以直接打字', note('unavailable')),
  frame('m-interrupted', '× · 录音中断（断网、语音服务断开、App 切到后台）：文字保留，发送或重试；处理前不能再按住录音', hold('interrupted', { text: said.text + said.stash, seconds: 9 }), csoFixtures({ copy: false })),
  frame('n-keyboard-up', '规则 · 键盘弹起后：长按交给系统（光标、粘贴），不录音；提示不再显示', withKeyboard(<SystemEditMenu items={['粘贴', '自动填充']} />), csoFixtures({ copy: false })),
  frame('n-has-text', '规则 · 输入框有字：长按交给系统（选择、粘贴），不录音', () => (
    <PhoneConversation overlay={<SystemEditMenu items={['选择', '全选', '粘贴']} />} />
  ), csoFixtures({ draft: typed })),
  frame('o-session', '普通会话也一样：发到这个会话', () => (
    <PhoneConversation overlay={<ComposerHoldLayer device="phone" to={{ session: '修复中途断缓存' }} phase="recording" text={saidSession.text} stash={saidSession.stash} seconds={4} />} />
  ), sessionFixtures(), [390, 844], '/chat/ses_demo_cache'),
  frame('p-idle-small', '1 · 静止 · 小屏 375×667', () => <PhoneConversation />, csoFixtures(), small),
  frame('q-recording-small', '4 · 录音中 · 小屏 375×667', hold('recording', { text: said.text, stash: said.stash, seconds: 6 }), csoFixtures(), small),
  frame('r-armed-small', '5 · 取消待定 · 小屏 375×667', hold('armed', { text: said.text, stash: said.stash, seconds: 7 }), csoFixtures(), small),
]);

export default designFrames;
