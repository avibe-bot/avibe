// Agent reply Session & Bot mentions across Web/Desktop, Phone (390 wide), and IM (Slack).
// Demonstrates:
// - a resolved Session with a short title (clickable SessionMentionChip)
// - a resolved Session with a very long title (ellipsis truncation, unchanged line height)
// - a Bot mention beside it (BotMentionChip with avatar)
// - an id that does not resolve (exact plain text, no broken chip)
// - the same reply formatted as a Slack IM message (clickable title links with public URL,
//   and title alone when no public address is configured).
import { frames } from '../../frame';
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

function SlackMentionPreview() {
  return (
    <div className="flex min-h-full flex-col gap-4 bg-canvas p-5 text-foreground">
      <div className="rounded-xl border border-edge bg-surface p-4 shadow-sm">
        <div className="mb-3 flex items-center justify-between border-b border-edge pb-2">
          <div className="flex items-center gap-2">
            <span className="inline-flex size-6 items-center justify-center rounded-md bg-cyan-soft text-[12px] font-bold text-cyan-ink">
              #
            </span>
            <span className="text-[13px] font-semibold">
              {tText('Slack · 已配置公网地址（标题带会话链接）', 'Slack · Public URL configured (linked titles)')}
            </span>
          </div>
          <span className="font-mono text-[11px] text-muted">#avibe-ops</span>
        </div>

        <div className="flex items-start gap-3">
          <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-mint-soft text-[12px] font-bold text-mint-ink">
            A
          </div>
          <div className="min-w-0 flex-1 space-y-1.5">
            <div className="flex items-baseline gap-2">
              <span className="text-[13px] font-semibold text-foreground">Avibe</span>
              <span className="rounded bg-field px-1.5 py-0.5 text-[10px] font-medium text-muted">APP</span>
              <span className="text-[11px] text-muted">00:54</span>
            </div>
            <p className="text-[13px] leading-[1.6] text-foreground">
              {isEn ? (
                <>
                  Done — I started a background Agent (
                  <a href="#slack-short" className="text-cyan-ink underline underline-offset-2">
                    {SHORT_TITLE}
                  </a>
                  ), handed the reconnect audit to{' '}
                  <a href="#slack-long" className="text-cyan-ink underline underline-offset-2">
                    {LONG_TITLE}
                  </a>
                  , and looped in @{BOT_NAME} for review. The stale id #&lt;sesmissing99&gt; from the log does not
                  resolve here so it stays plain text, and inline code{' '}
                  <code className="rounded border border-edge bg-field px-1 py-0.5 font-mono text-[11px]">
                    #&lt;sesw34zgzgcvp&gt;
                  </code>{' '}
                  is left untouched.
                </>
              ) : (
                <>
                  好，已经起了一个后台 Agent（
                  <a href="#slack-short" className="text-cyan-ink underline underline-offset-2">
                    {SHORT_TITLE}
                  </a>
                  ），并把长链路排查交给了{' '}
                  <a href="#slack-long" className="text-cyan-ink underline underline-offset-2">
                    {LONG_TITLE}
                  </a>
                  ，同时抄送 @{BOT_NAME} 跟进验收。另外日志里提到的旧编号 #&lt;sesmissing99&gt;
                  在当前工作区找不到，所以保留原样；文档里的示例{' '}
                  <code className="rounded border border-edge bg-field px-1 py-0.5 font-mono text-[11px]">
                    #&lt;sesw34zgzgcvp&gt;
                  </code>{' '}
                  也不会被改写。
                </>
              )}
            </p>
            <div className="pt-1 font-mono text-[11px] text-muted">
              https://demo.avibe.bot/chat/sesw34zgzgcvp · https://demo.avibe.bot/chat/seslongtitle1
            </div>
          </div>
        </div>
      </div>

      <div className="rounded-xl border border-edge bg-surface p-4 shadow-sm">
        <div className="mb-2 flex items-center justify-between border-b border-edge pb-2">
          <span className="text-[12px] font-semibold text-muted">
            {tText(
              '无公网地址或不支持链接的平台（如微信）· 仅显示标题',
              'No public URL or plain-text platform (e.g. WeChat) · Title alone',
            )}
          </span>
        </div>
        <p className="text-[13px] leading-[1.6] text-foreground">
          {isEn
            ? `Done — I started a background Agent (${SHORT_TITLE}), handed the reconnect audit to ${LONG_TITLE}, and looped in @${BOT_NAME} for review. The stale id #<sesmissing99> stays plain text.`
            : `好，已经起了一个后台 Agent（${SHORT_TITLE}），并把长链路排查交给了 ${LONG_TITLE}，同时抄送 @${BOT_NAME} 跟进验收。旧编号 #<sesmissing99> 保留原样。`}
        </p>
      </div>
    </div>
  );
}

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
    status: 'shipped',
    themes: ['light'],
    fixtures: imFixtures,
    render: () => <SlackMentionPreview />,
  },
]);

export default designFrames;
