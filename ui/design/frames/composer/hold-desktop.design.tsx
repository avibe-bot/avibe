// Hold the composer to talk, on a desktop (owner direction, 2026-11-02). A press in the empty
// composer shows a ring at the pointer after 100 ms; it fills by 350 ms and the microphone opens.
// Moving the pointer more than 6 px first makes it an ordinary click. Releasing sends to this
// conversation; leaving the composer arms cancel, Esc cancels at once. The mic button and ⌥Z
// stay as they are: press to start, press again to finish, the text goes into the draft.
// Every part is shipped; a frame sets the hold's state the way `useHoldToTalk` reports it.
import type { ReactNode } from 'react';

import type { ComposerHoldView } from '@/components/voice/ComposerHold';
import { stillVoiceLevel } from '@/lib/voiceLevel';

import { frames } from '../../frame';
import { DesktopScreen } from '../../shell/DesktopScreen';
import type { WorkbenchFixtures } from '../../shell/fixtures';
import { HeldComposer, TextPressLayer, type HeldTarget } from '../../shell/HeldComposer';
import { speakingMeter } from '../../shell/meter';
import { csoFixtures, csoSentMessages, said, saidLong, saidSession, sessionFixtures, staged, typed } from './fixtures';

const cso: HeldTarget = { bot: 'cso' };

const frame = (name: string, title: string, render: () => ReactNode, fixtures: WorkbenchFixtures = csoFixtures(), route = '/chat/ses_cso') => ({
  name,
  title,
  width: 1440,
  height: 900,
  status: 'shipped' as const,
  fixtures,
  route,
  render,
});

const hold = (state: Partial<ComposerHoldView>, extra: { attachments?: string[]; to?: HeldTarget } = {}) => () => (
  <DesktopScreen overlay={<HeldComposer pointer="mouse" to={extra.to ?? cso} hold={state} attachments={extra.attachments} />} />
);

const recording = { phase: 'recording', text: said.text, stash: said.stash, seconds: 6 } as const;

const designFrames = frames([
  frame('a-idle', '1 · 静止：输入框为空，占位提示「给首席安全官发消息，或按住说话」（普通会话是「输入消息，或按住说话」）', () => <DesktopScreen />),
  frame('b-pressing', '2 · 按下 100ms 后出现圆环，到 350ms 填满（期间移动 >6px = 普通点击）', hold({ phase: 'pressing', progress: 0.55 })),
  frame('b-pressing-1', '2a · 圆环刚出现', hold({ phase: 'pressing', progress: 0.2 })),
  frame('b-pressing-3', '2c · 圆环将满', hold({ phase: 'pressing', progress: 0.9 })),
  frame('c-listening', '3 · 满 350ms：麦克风打开，还没出字', hold({ phase: 'listening', seconds: 1 })),
  frame('d-recording', '4 · 录音中：实时转写在输入框上方，波形是麦克风的真实音量；松开发送', hold(recording)),
  frame('d-recording-reduced', '4d · 系统开了「减少动态效果」：只有一条静态音量条', hold({ ...recording, level: stillVoiceLevel(speakingMeter, { reducedMotion: true }) })),
  frame('d-recording-no-live', '4e · 实时转写连不上（或说了几秒话却一直没出字）：卡片安静地说明，松手后整段识别，说的话都留着', hold({ phase: 'recording', seconds: 23, liveUnavailable: true })),
  frame('d-recording-long', '4b · 说得很长：转写区滚动，停在最新一行，不限时长', hold({ phase: 'recording', text: saidLong.text, stash: saidLong.stash, seconds: 48 })),
  frame('d-recording-attachment', '4c · 只放了附件、没有文字：照样按住录音，附件和语音一起发', hold(recording, { attachments: [staged] })),
  frame('e-armed', '5 · 指针移出输入框：波形压平变红，松开取消（移回来继续说；按 Esc 直接取消）', hold({ ...recording, phase: 'armed', seconds: 7 })),
  frame('f-settling', '6 · 松开：整理最终文字（最长 20 秒），浅薄荷色的波从左扫到右，输入框暂时锁住；行末灰色的叉（或 Esc）立即停下、不发送', hold({ phase: 'settling', text: said.final, seconds: 8 })),
  frame('g-sent', '7 · 已发送：消息直接进了对话，带「语音」标记（伙伴对话不排队），首席安全官开始工作；占位是「首席安全官 正在处理…」，照样可以按住说话', () => <DesktopScreen />, csoFixtures({ messages: csoSentMessages, running: true })),
  frame('h-cancelled', '× · 取消（或整理中点叉 / Esc，录音不到 3 秒或没听到说话）：没有发送', hold({ outcome: 'cancelled' })),
  frame('h-cancelled-kept', '× · 整理中点了叉或按 Esc，录音里有说话、超过 3 秒：没有发送，录音留在这台设备上，稍后在输入框上方重试', hold({ outcome: 'kept' })),
  frame('i-too-short', '× · 太短（不到 0.5 秒）或没听到说话：没有发送', hold({ outcome: 'too-short' })),
  frame('j-asking', '× · 第一次用：浏览器询问麦克风权限，这一次不录音', hold({ outcome: 'asking' })),
  frame('k-denied', '× · 麦克风被拒绝：说明去哪里打开，可以直接打字', hold({ outcome: 'denied' })),
  frame('l-unavailable', '× · 语音服务暂时不可用：可以直接打字', hold({ outcome: 'unavailable' })),
  frame('m-interrupted', '× · 录音中断（断网、语音服务断开、窗口失焦）：文字保留，发送或重试；处理前不能再按住录音', hold({ phase: 'interrupted', interruptReason: 'dropped', text: said.text + said.stash, seconds: 9 }), csoFixtures({ hint: false })),
  frame('m-interrupted-empty', '× · 中断时一个字都还没转出来（实时转写一直没连上，回退也失败）：卡片如实说「还没有文字」，录音保留，只能重试或不要', hold({ phase: 'interrupted', interruptReason: 'dropped', seconds: 142 }), csoFixtures({ hint: false })),
  frame('n-has-text', '规则 · 输入框里有字：按住就是选字，不录音（占位提示随文字消失）', () => <DesktopScreen overlay={<TextPressLayer />} />, csoFixtures({ draft: typed })),
  frame('o-session', '普通会话也一样：发到这个会话', hold({ phase: 'recording', text: saidSession.text, stash: saidSession.stash, seconds: 4 }, { to: { session: '修复中途断缓存' } }), sessionFixtures(), '/chat/ses_demo_cache'),
]);

export default designFrames;
