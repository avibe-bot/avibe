// Agent reply Session & Bot mentions across Web/Desktop, Phone (390 wide), and IM (Slack).
// Demonstrates:
// - a resolved Session with a short title (clickable SessionMentionChip)
// - a resolved Session with a very long title (ellipsis truncation, unchanged line height)
// - a Bot mention beside it (BotMentionChip with avatar)
// - an id that does not resolve (exact plain text, no broken chip)
// - the same reply formatted as a Slack IM message (clickable title links with public URL,
//   and title alone when no public address is configured).
import { frames } from '../../frame';
import { SlackMentionPreview } from '../../kit/im-mention-preview';
import { bot, demoBots } from '../../shell/fixtures';
import { settingsConfig } from '../settings/fixtures';
import { workbenchFixtures } from '../workbench/fixtures';
import {
  agentSays,
  chatDesktop,
  chatFixtures,
  chatPhone,
  chatRoute,
  userSays,
} from './fixtures';

const isEn = typeof window !== 'undefined' && new URLSearchParams(window.location.search).get('lang') === 'en';
const tText = (zh: string, en: string) => (isEn ? en : zh);

const SHORT_TITLE = tText('登录跳转修复', 'Auth redirect fix');
const LONG_TITLE = tText(
  '移动端 WebView 切换网络后的 SSE 断线重连与本地消息同步核对清单',
  'Mobile WebView SSE reconnect and local message sync verification checklist after network switch',
);
const BOT_NAME = tText('产品伙伴', 'Product Partner');

const mentionBots = [
  ...demoBots,
  bot('product', BOT_NAME, tText('产品方向、体验与研发协调', 'Product direction and delivery coordination'), {
    kind: 'emoji',
    emoji: '🧭',
    hue: 165,
  }),
];

const conversation = [
  userSays(
    tText(
      '把登录跳转的修复和 WebView 弱网重连核对都派出去，顺便抄送产品伙伴。',
      'Kick off both the auth redirect fix and the WebView reconnect audit, and loop in Product Partner.',
    ),
    12,
  ),
  agentSays(
    tText(
      '好，已经起了一个后台 Agent（#<sesw34zgzgcvp>），并把长链路排查交给了 #seslongtitle1，同时抄送 @<bot:product> 跟进验收。另外日志里提到的旧编号 #<sesmissing99> 在当前工作区找不到，所以保留原样；文档里的示例 `#<sesw34zgzgcvp>` 也不会被改写。',
      'Done — I started a background Agent (#<sesw34zgzgcvp>), handed the reconnect audit to #seslongtitle1, and looped in @<bot:product> for review. The stale id #<sesmissing99> from the log does not resolve here so it stays plain text, and inline code `#<sesw34zgzgcvp>` is left untouched.',
    ),
    8,
    {
      content: {
        references: [
          { kind: 'session', session_id: 'sesw34zgzgcvp', title: SHORT_TITLE, openable: true },
          { kind: 'session', session_id: 'seslongtitle1', title: LONG_TITLE, openable: true },
          { kind: 'bot', handle: 'product', display_name: BOT_NAME },
        ],
      },
    },
  ),
];

const mentionChatFixtures = chatFixtures({
  session: { title: tText('后台任务派发与跟进', 'Background task dispatch') },
  messages: conversation,
  api: { '/api/config': { ...settingsConfig, language: undefined } },
  extra: { bots: mentionBots },
});

const imFixtures = workbenchFixtures({
  '/api/config': { ...settingsConfig, language: undefined },
});

const designFrames = frames([
  {
    name: 'desktop',
    title: '会话 · Agent 回复里的 Session 与 Bot 引用 · 桌面',
    width: 1192,
    height: 760,
    status: 'shipped',
    themes: ['light'],
    route: chatRoute(),
    fixtures: mentionChatFixtures,
    render: chatDesktop,
  },
  {
    name: 'phone',
    title: '会话 · Agent 回复里的 Session 与 Bot 引用 · 手机 390',
    width: 390,
    height: 760,
    status: 'shipped',
    themes: ['light'],
    route: chatRoute(),
    fixtures: mentionChatFixtures,
    render: chatPhone,
  },
  {
    name: 'im-slack',
    title: 'IM · Slack 上的同一条 Agent 回复',
    width: 560,
    height: 460,
    status: 'proposed',
    themes: ['light'],
    fixtures: imFixtures,
    render: () => (
      <SlackMentionPreview
        isEn={isEn}
        shortTitle={SHORT_TITLE}
        longTitle={LONG_TITLE}
        botName={BOT_NAME}
      />
    ),
  },
]);

export default designFrames;
