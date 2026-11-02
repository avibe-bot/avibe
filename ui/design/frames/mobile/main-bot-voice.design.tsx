// The center of the bar is the main Bot (owner direction, 2026-11-02). Tap opens its
// conversation; press and hold records a voice message that goes straight to it,
// with the live transcript from the shipped dictation pipeline; sliding up cancels.
// One frame per state, in the order a press goes through them, then the outcomes.
import { frames } from '../../frame';
import { BotConversationHeader } from '../../kit/BotConversationHeader';
import { VoiceHoldOverlay, VoiceToast } from '../../kit/MainBotVoice';
import { WorkbenchTabBar, WorkbenchTabPage } from '../../kit/WorkbenchTab';
import { botConversationApi } from '../../shell/fixtures';
import { PhoneConversation, PhoneScreen } from '../../shell/PhoneScreen';
import { afterVoiceTranscript, agentsApi, mainRunningFixtures, spoken, team, teamFixtures } from '../../shell/teamFixtures';
import type { CenterPhase } from '../../kit/BotCenterTabBar';
import type { ReactNode } from 'react';

const screen = (phase: CenterPhase, overlay?: ReactNode, inboxBadge = 6) => (
  <PhoneScreen tabBar={<WorkbenchTabBar inboxBadge={inboxBadge} phase={phase} />} overlay={overlay}>
    <WorkbenchTabPage />
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
  status: 'proposed' as const,
  fixtures,
  route,
  render,
});

const designFrames = frames([
  frame('a-idle', '1 · 静止：歪比有 1 条未读', () => screen('idle')),
  frame('a-idle-running', '1b · 静止：歪比在工作', () => screen('idle', undefined, 5), mainRunningFixtures),
  frame('b-pressed', '2 · 按下（未到 300ms：松开 = 点一下，打开对话）', () => screen('pressed')),
  frame('c-listening', '3 · 按住满 300ms：开始录音，还没听到字', () => screen('recording', <VoiceHoldOverlay phase="listening" seconds={1} />)),
  frame('d-recording', '4 · 录音中：实时转写，上滑取消 · 松开发送', () => screen('recording', <VoiceHoldOverlay phase="recording" text={spoken.text} stash={spoken.stash} seconds={6} />)),
  frame('e-armed', '5 · 手指滑进取消区：松开取消', () => screen('armed', <VoiceHoldOverlay phase="armed" text={spoken.text} stash={spoken.stash} seconds={7} />)),
  frame('f-limit', '5b · 接近 60 秒上限：倒数，满 60 秒自动发送', () => screen('recording', <VoiceHoldOverlay phase="limit" text={`${spoken.final}另外把发布清单里没勾的几项列给我，按负责人分一下，`} stash="今天下班前" seconds={52} />)),
  frame('g-settling', '6 · 松开：最终文字整理中（最长 20 秒）', () => screen('settling', <VoiceHoldOverlay phase="settling" text={spoken.final} seconds={8} />)),
  frame('h-sent', '7 · 已发给歪比：留在原页面，歪比开始工作', () => screen('idle', <VoiceToast outcome="sent" text={spoken.final} />, 5), sentFixtures),
  frame('i-conversation', '8 · 点「查看」或点歪比：消息在对话里，歪比在回', () => <PhoneConversation overlay={<BotConversationHeader current="main" />} />, conversationFixtures, [390, 844], '/chat/ses_main'),
  frame('j-cancelled', '× · 在取消区松开：没有发送', () => screen('idle', <VoiceToast outcome="cancelled" />)),
  frame('k-too-short', '× · 太短或没听到说话', () => screen('idle', <VoiceToast outcome="too-short" />)),
  frame('l-denied', '× · 没有麦克风权限', () => screen('idle', <VoiceToast outcome="denied" />)),
  frame('m-unavailable', '× · 语音服务不可用（需要 Avibe Cloud）', () => screen('idle', <VoiceToast outcome="unavailable" />)),
  frame('n-failed', '× · 转写失败：录音保留，可重试', () => screen('idle', <VoiceToast outcome="failed" />)),
  frame('o-recording-small', '4 · 录音中 · 小屏 375×667', () => screen('recording', <VoiceHoldOverlay phase="recording" text={spoken.text} stash={spoken.stash} seconds={6} />), teamFixtures, [375, 667]),
]);

export default designFrames;
