// The composer and the queue strip above it, rendered by the shipped ChatPage: a saved draft with voice
// input offered, messages queued behind a running turn (one carrying a screenshot), and a queue held for an
// explicit retry after the turn before it failed.
import regionUrl from '../../assets/annotation-region.svg';
import { frames } from '../../frame';
import { agentSays, chatDesktop, chatFixtures, chatPhone, chatRoute, failureApi, failureNotice, message, runningTurn, userSays } from './fixtures';

const messages = [
  userSays('Inbox 的未读数和侧边栏对不上，帮我查一下。', 26),
  agentSays('找到了：侧边栏只数了项目会话，伙伴的未读没算进去。改成从同一个 `unreadByCategory` 读，两边就一致了。要我直接改吗？', 22),
];

const draft = '改吧，顺便把手机上的 Tab 角标也一起对齐。\n改完跑一下 InboxPage 和 WorkbenchSidebar 的单测。';

const queued = (text: string, minutesAgo: number, extra = {}) => message('user', 'queued', text, minutesAgo, { state: 'queued', ...extra });

const waiting = [
  queued('顺便把手机上的 Tab 角标也一起对齐。', 3),
  queued('这是现在手机上的样子', 1, {
    content: { attachments: [{ url: '/api/media/tok_region', name: 'inbox-badge.png', mime: 'image/png', kind: 'image', width: 640, height: 300 }] },
  }),
];

const held = [
  queued('先别动 Tab 角标，等设计确认。', 4, { requires_explicit_retry: true, retry_reason: 'backend_failure' }),
];

const api = { '/api/media/tok_region': regionUrl };

const designFrames = frames([
  {
    name: 'draft',
    title: '输入框 · 有草稿，可语音输入',
    width: 1192,
    height: 640,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ session: { title: 'Inbox 未读数' }, messages, draft, voice: true }),
    render: chatDesktop,
  },
  {
    name: 'route-picker',
    title: '输入框 · Agent 与模型',
    width: 1192,
    height: 720,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ session: { title: 'Inbox 未读数' }, messages }),
    click: ['button:has([data-route-trigger-parts])'],
    render: chatDesktop,
  },
  {
    name: 'queued',
    title: '输入框 · Agent 运行中，两条消息在排队',
    width: 1192,
    height: 720,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ session: { title: 'Inbox 未读数' }, messages, queued: waiting, turnState: runningTurn, api }),
    render: chatDesktop,
  },
  {
    name: 'retry-required',
    title: '输入框 · 上一轮失败，排队的消息等你重试',
    width: 1192,
    height: 720,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({
      session: { title: 'Inbox 未读数' },
      messages: [
        ...messages,
        failureNotice('turn_q1', 5),
      ],
      queued: held,
      api: failureApi('turn_q1'),
    }),
    render: chatDesktop,
  },
  {
    name: 'phone-draft',
    title: '输入框 · 有草稿，可语音输入 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ session: { title: 'Inbox 未读数' }, messages, draft, voice: true }),
    render: chatPhone,
  },
  {
    name: 'phone-route-picker',
    title: '输入框 · Agent 与模型 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ session: { title: 'Inbox 未读数' }, messages }),
    click: ['button:has([data-route-trigger-parts])'],
    render: chatPhone,
  },
  {
    name: 'phone-queued',
    title: '输入框 · Agent 运行中，消息在排队 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ session: { title: 'Inbox 未读数' }, messages, queued: waiting, turnState: runningTurn, api }),
    render: chatPhone,
  },
]);

export default designFrames;
