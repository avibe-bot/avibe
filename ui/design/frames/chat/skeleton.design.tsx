// A conversation opened before its messages arrived (ui/design: system/skeleton/*). What the app
// already knows is drawn as itself (the header with its menu and Show Page control, the composer with
// Attach, the Agent's route, and Send); only the transcript is blocks. `from-inbox`: opened from an
// Inbox row, whose preview is the newest Agent reply, so that reply is the real bubble at the bottom.
// Beside each, the same conversation once its messages arrived, at the same size. ChatPage is another
// lane's; `kit/ChatSkeleton` is the look it is asked to build.
import { frames } from '../../frame';
import { ChatSkeleton } from '../../kit/ChatSkeleton';
import { demoAgents } from '../agents/fixtures';
import { consolePage, phoneShell } from '../workbench/fixtures';
import { agentSays, chatDesktop, chatFixtures, chatPhone, chatRoute, chatSession, userSays } from './fixtures';

const messages = [
  userSays('帮我把 avibe 工作台适配移动端，交互可以和 PC 不一样，但功能要完备。', 62),
  agentSays(
    '已经读完现有 Web UI 代码。结论：Workbench 在手机上目前没有导航，只能靠返回键。方案是给它一套底部 Tab：收件箱、项目、新建会话、应用。聊天页和搜索页全屏，进入时隐藏 Tab。',
    55,
  ),
  userSays('底部这几个入口没问题，继续，先做项目页。', 30),
  agentSays('项目页做好了：项目按最近活跃排序，点开是会话列表，长按可以置顶。改动在 `AppShell.tsx` 和新的 `ProjectsPage.tsx`，单测和 e2e 都过了。', 12),
];

// A longer conversation, opened from its Inbox card: the newest reply is that card's preview.
const latest = '收件箱也做好了：伙伴在前、会话在后，各自按最近活跃排序；未读的卡片有薄荷色描边，点「打开会话」直接进对话。PR #3141 已开，等你 review。';
const history = [
  ...messages,
  userSays('不错。收件箱也按这个思路做，伙伴放在会话前面。', 10),
  agentSays('好的，先确认两点：伙伴的未读是否也算进总数？会话卡片要不要显示项目名？', 9),
  userSays('都要。项目名放在标题前面，用小标签。', 8),
  agentSays(latest, 2),
];

const session = chatSession();
const opening = <ChatSkeleton session={session} agents={demoAgents} />;
const fromInbox = <ChatSkeleton session={session} agents={demoAgents} latest={latest} />;

const designFrames = frames([
  {
    name: 'desktop',
    title: '会话 · 骨架 · 桌面',
    width: 1192,
    height: 900,
    status: 'approved',
    route: chatRoute(),
    fixtures: chatFixtures({ messages }),
    render: consolePage(opening),
  },
  {
    name: 'desktop-loaded',
    title: '会话 · 骨架对照 · 桌面消息到位',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ messages }),
    render: chatDesktop,
  },
  {
    name: 'phone',
    title: '会话 · 骨架 · 手机',
    width: 390,
    height: 844,
    status: 'approved',
    route: chatRoute(),
    fixtures: chatFixtures({ messages }),
    render: phoneShell('/chat/:sessionId', opening),
  },
  {
    name: 'phone-loaded',
    title: '会话 · 骨架对照 · 手机消息到位',
    width: 390,
    height: 844,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ messages }),
    render: chatPhone,
  },
  {
    name: 'from-inbox',
    title: '会话 · 骨架 · 从收件箱进入（最新回复已知）· 桌面',
    width: 1192,
    height: 900,
    status: 'approved',
    route: chatRoute(),
    fixtures: chatFixtures({ messages: history }),
    render: consolePage(fromInbox),
  },
  {
    name: 'from-inbox-loaded',
    title: '会话 · 骨架对照 · 从收件箱进入 · 桌面消息到位',
    width: 1192,
    height: 900,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ messages: history }),
    render: chatDesktop,
  },
  {
    name: 'phone-from-inbox',
    title: '会话 · 骨架 · 从收件箱进入（最新回复已知）· 手机',
    width: 390,
    height: 844,
    status: 'approved',
    route: chatRoute(),
    fixtures: chatFixtures({ messages: history }),
    render: phoneShell('/chat/:sessionId', fromInbox),
  },
  {
    name: 'phone-from-inbox-loaded',
    title: '会话 · 骨架对照 · 从收件箱进入 · 手机消息到位',
    width: 390,
    height: 844,
    status: 'shipped',
    route: chatRoute(),
    fixtures: chatFixtures({ messages: history }),
    render: chatPhone,
  },
]);

export default designFrames;
