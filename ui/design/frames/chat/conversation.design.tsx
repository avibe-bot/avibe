// The chat conversation: the header, the transcript, and the composer, rendered by the shipped ChatPage.
// ui/design: chat/conversation/* — the empty states, a Bot's conversation, a fork, an archived chat, and the
// named senders of an Organization instance's shared transcript.
import { frames } from '../../frame';
import { agentSays, at, chatDesktop, chatFixtures, chatPhone, chatRoute, chatSession, userSays } from './fixtures';

const messages = [
  userSays('帮我把 avibe 工作台适配移动端，交互可以和 PC 不一样，但功能要完备。', 62),
  agentSays(
    '已经读完现有 Web UI 代码。结论：Workbench 在手机上目前没有导航，只能靠返回键。方案是给它一套底部 Tab：\n\n- **收件箱**：有新回复的会话\n- **项目**：文件夹视图\n- **＋**：新建会话\n- **应用**：Dock 里的应用和更多\n\n聊天页和搜索页全屏，进入时隐藏 Tab。',
    55,
  ),
  userSays('底部这几个入口没问题，继续，先做项目页。', 30),
  agentSays(
    '项目页做好了：项目按最近活跃排序，点开是会话列表，长按可以置顶。底部 Tab 在 768 以下出现：\n\n```tsx\n<MobileTabBar items={workbenchTabs} center={newSession} />\n```\n\n改动在 `AppShell.tsx` 和新的 `ProjectsPage.tsx`，单测和 e2e 都过了。下一步做什么？',
    12,
    { content: { quick_replies: ['继续做收件箱', '先开 PR'] } },
  ),
];

const bot = chatSession({ id: 'ses_cso', project_id: null, scope_id: null, title: '首席安全官', agent_name: 'cso' });
// The main Bot, whose Agent description is written for Agents and never shows here.
const mainBot = chatSession({ id: 'ses_main', project_id: null, scope_id: null, title: null, agent_name: 'main' });

const archived = [
  userSays('把 avault 升到 0.7.0，跑一遍依赖审计。', 60 * 26),
  agentSays('升级完成，`cargo audit` 和 `npm audit` 都没有高危项。PR #3088 已合并，这个会话可以归档了。', 60 * 25),
];

// An Organization instance: several people share the transcript, so each human bubble names its sender.
const shared = [
  userSays('这周的发版清单整理一下，先列阻塞项。', 50, { author_id: 'remote:u_lin', sender_label: 'lin@acme.dev' }),
  agentSays('阻塞项有 2 个：\n\n1. Composer 语音输入在 iOS 上丢第一段（#912）\n2. Show Page 分享弹层在手机上被裁切（#941）\n\n其余 6 项都已合并。', 46),
  userSays('#941 我来修，今天下班前提 PR。', 20, { author_id: 'remote:u_zhou', sender_label: 'zhou@acme.dev' }),
  userSays('好，#912 先标成发版后修复。', 8, { author_id: 'remote:u_me', sender_label: 'me@acme.dev' }),
];

const designFrames = frames([
  {
    name: 'default',
    title: '会话 · 对话中',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ messages }),
    render: chatDesktop,
  },
  {
    name: 'session-menu',
    title: '会话 · 会话菜单',
    width: 1192,
    height: 640,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ messages: messages.slice(0, 2) }),
    click: ['role=button[name="{t:workbench.sessionActions}"]'],
    render: chatDesktop,
  },
  {
    name: 'empty',
    title: '会话 · 新会话还没有消息',
    width: 1192,
    height: 640,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ session: { title: null } }),
    render: chatDesktop,
  },
  {
    name: 'bot',
    title: '会话 · 伙伴的对话还没有消息',
    width: 1192,
    height: 640,
    status: 'shipped',
    route: chatRoute(bot.id),
    fixtures: chatFixtures({ session: bot }),
    render: chatDesktop,
  },
  {
    name: 'bot-main',
    title: '会话 · 主伙伴的对话还没有消息',
    width: 1192,
    height: 640,
    status: 'shipped',
    route: chatRoute(mainBot.id),
    fixtures: chatFixtures({ session: mainBot }),
    render: chatDesktop,
  },
  {
    name: 'forked',
    title: '会话 · 从另一个会话分叉',
    width: 1192,
    height: 640,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({
      session: {
        title: '收件箱分类设计（分叉）',
        metadata: { created_via: 'session_fork', fork_source_session_id: 'sesq4n8c1wz0e', fork_source_session_title: '收件箱分类设计' },
      },
    }),
    render: chatDesktop,
  },
  {
    name: 'archived',
    title: '会话 · 已归档（只读）',
    width: 1192,
    height: 640,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ session: { title: 'avault 依赖升级', status: 'archived', updated_at: at(60 * 25) }, messages: archived }),
    render: chatDesktop,
  },
  {
    name: 'organization',
    title: '会话 · 组织实例的共享会话（标出发送人）',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({
      session: { title: '发版清单' },
      messages: shared,
      extra: { instanceKind: 'organization', readerPrincipal: 'remote:u_me' },
    }),
    render: chatDesktop,
  },
  {
    name: 'phone',
    title: '会话 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ messages }),
    render: chatPhone,
  },
  {
    name: 'phone-bot',
    title: '会话 · 伙伴的对话 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: chatRoute(bot.id),
    fixtures: chatFixtures({ session: bot }),
    render: chatPhone,
  },
  {
    name: 'phone-bot-main',
    title: '会话 · 主伙伴的对话 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: chatRoute(mainBot.id),
    fixtures: chatFixtures({ session: mainBot }),
    render: chatPhone,
  },
]);

export default designFrames;
