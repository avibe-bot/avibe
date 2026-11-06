// The center of the bar is the main Bot (owner decisions, 2026-11-02). Tap opens its
// conversation; press and hold records a voice message that goes straight to it,
// with the live transcript from the shipped dictation pipeline; sliding up cancels.
// No time limit; a long transcript scrolls; a take cut off keeps what was heard.
// After sending the user stays put (the toast's 查看 opens the conversation), and the
// message is marked as voice. One frame per state, in press order, then the outcomes.
// Every part is shipped; a frame sets the hold's state the way `useHoldToTalk` reports it.
import type { ReactNode } from 'react';

import type { BotCenterPhase } from '@/components/MobileChrome';
import type { HoldView } from '@/components/voice/MainBotVoice';
import { stillVoiceLevel } from '@/lib/voiceLevel';
import { ProjectsPage } from '@/components/workbench/ProjectsPage';

import { frames } from '../../frame';
import { botConversationApi } from '../../shell/fixtures';
import { speakingMeter } from '../../shell/meter';
import { BotCenterTabBar, MainBotVoiceLayer, PhoneConversation, PhoneScreen } from '../../shell/PhoneScreen';
import { afterVoiceTranscript, agentsApi, mainRunningFixtures, spoken, team, teamFixtures } from '../../shell/teamFixtures';

const screen = (phase: BotCenterPhase, hold?: Partial<HoldView>, inboxBadge = 6) => (
  <PhoneScreen tabBar={<BotCenterTabBar inboxBadge={inboxBadge} phase={phase} />} overlay={hold && <MainBotVoiceLayer hold={hold} />}>
    <ProjectsPage />
  </PhoneScreen>
);

// Once the message is in, the main Bot is working on it and its earlier reply is read.
const sentFixtures = {
  ...mainRunningFixtures,
  unreadBySession: { ses_cso: 2, ses_demo_cache: 3 },
};
const conversationFixtures = {
  ...sentFixtures,
  api: { ...agentsApi, ...botConversationApi(team[0], team, afterVoiceTranscript, true) },
};

const frame = (name: string, title: string, render: () => ReactNode, fixtures = teamFixtures, size: [number, number] = [390, 844], route = '/projects') => ({
  name,
  title,
  width: size[0],
  height: size[1],
  status: 'shipped' as const,
  fixtures,
  route,
  render,
});

const recording = { phase: 'recording', text: spoken.text, stash: spoken.stash, seconds: 6 } as const;

const designFrames = frames([
  frame('a-idle', '1 · 静止：歪比有 1 条未读', () => screen('idle')),
  frame('a-idle-running', '1b · 静止：歪比在工作', () => screen('idle', undefined, 5), mainRunningFixtures),
  frame('b-pressed', '2 · 按下 100ms 后歪比按下去（未到 200ms：松开 = 点一下，打开对话）', () => screen('pressed')),
  frame('c-listening', '3 · 按住满 200ms：开始录音，还没听到字；波形只有很轻的呼吸', () => screen('recording', { phase: 'listening', seconds: 1 })),
  frame('d-recording', '4 · 录音中：实时转写，波形是麦克风的真实音量（最新在右）；上滑取消 · 松开发送', () => screen('recording', recording)),
  frame('d-recording-reduced', '4c · 系统开了「减少动态效果」：只有一条静态音量条', () => screen('recording', { ...recording, level: stillVoiceLevel(speakingMeter, { reducedMotion: true }) })),
  frame('e-armed', '5 · 手指滑进取消区：波形压平变红，松开取消', () => screen('armed', { ...recording, phase: 'armed', seconds: 7 })),
  frame('f-long', '5b · 说得很长：不限时长，转写在卡片里滚动，停在最新一行', () => screen('recording', { phase: 'recording', text: spoken.long, stash: '顺便提醒产品经理更新一下对外的发布说明', seconds: 95 })),
  frame('g-settling', '6 · 松开：最终文字整理中（最长 20 秒），一道浅薄荷色的波从左扫到右；行末灰色的叉立即停下、不发送', () => screen('settling', { phase: 'settling', text: spoken.final, seconds: 8 })),
  frame('h-sent', '7 · 已发给歪比：留在原页面，歪比开始工作', () => screen('idle', { outcome: 'sent', text: spoken.final }, 5), sentFixtures),
  frame('i-conversation', '8 · 点「查看」或点歪比：消息在对话里，标着「语音」，歪比在回', () => <PhoneConversation />, conversationFixtures, [390, 844], '/chat/ses_main'),
  frame('j-cancelled', '× · 在取消区松开（或整理中点叉，录音不到 3 秒或没听到说话）：没有发送', () => screen('idle', { outcome: 'cancelled' })),
  frame('j-cancelled-kept', '× · 整理中点了叉，录音里有说话、超过 3 秒：没有发送，录音留在这台设备上，可以在和歪比的对话里重试', () => screen('idle', { outcome: 'kept' })),
  frame('k-too-short', '× · 太短或没听到说话', () => screen('idle', { outcome: 'too-short' })),
  frame('l-denied', '× · 没有麦克风权限', () => screen('idle', { outcome: 'denied' })),
  frame('m-unavailable', '× · 语音服务不可用（需要 Avibe Cloud）', () => screen('idle', { outcome: 'unavailable' })),
  frame('n-failed', '× · 一个字都没识别出来：录音保留，可重试', () => screen('idle', { phase: 'interrupted', interruptReason: 'failed' })),
  frame('o-interrupted', '× · 中途断开（网络或语音服务）：已识别的文字保留，选发送或重试', () => screen('idle', { phase: 'interrupted', interruptReason: 'dropped', text: spoken.text + spoken.stash, seconds: 7 })),
  frame('p-recording-small', '4 · 录音中 · 小屏 375×667', () => screen('recording', recording), teamFixtures, [375, 667]),
  frame('q-asking', '× · 第一次按住：系统询问麦克风权限，这一次不录音', () => screen('idle', { outcome: 'asking' })),
]);

export default designFrames;
