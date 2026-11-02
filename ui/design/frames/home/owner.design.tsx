// Home, for the owner (owner direction, 2026-11-02): home is the main Bot's
// conversation. `/` opens /chat/ses_main, so the brand link, the strip's main Bot,
// and (on a phone) the app's start all land in one conversation, and the strip shows
// the main Bot as open. Empty, it greets by name with what the old home offered, and
// there is no project to pick. Everything but the welcome is the shipped page.
import { frames } from '../../frame';
import { BotConversationHeader } from '../../kit/BotConversationHeader';
import { WelcomeLayer } from '../../kit/MainBotWelcome';
import { DesktopScreen } from '../../shell/DesktopScreen';
import { botConversationApi, type WorkbenchFixtures } from '../../shell/fixtures';
import { PhoneConversation } from '../../shell/PhoneScreen';
import { agentsApi, mainRunningFixtures, mainTranscript, team, teamFixtures } from '../../shell/teamFixtures';

const [main] = team;

// A first visit: the main Bot alone, its conversation empty, nothing unread.
const firstFixtures: WorkbenchFixtures = {
  ...teamFixtures,
  bots: [main],
  unreadBySession: {},
  inboxSessions: [],
  api: { ...agentsApi, ...botConversationApi(main, [main], []) },
};
// Its conversation is open, so its replies are read.
const ongoingFixtures: WorkbenchFixtures = {
  ...teamFixtures,
  unreadBySession: { ses_cso: 2, ses_demo_cache: 3 },
  api: { ...agentsApi, ...botConversationApi(main, team, mainTranscript) },
};
const runningFixtures: WorkbenchFixtures = {
  ...mainRunningFixtures,
  api: { ...agentsApi, ...botConversationApi(main, team, mainTranscript, true) },
};

const desktop = { width: 1440, height: 900, status: 'proposed' as const, route: '/chat/ses_main' };
const phone = { width: 390, height: 844, status: 'proposed' as const, route: '/chat/ses_main' };

const designFrames = frames([
  {
    ...desktop,
    name: 'first-desktop',
    title: '首页 · 第一次（设置完成后）：歪比打招呼，没有项目要选',
    fixtures: firstFixtures,
    render: () => <DesktopScreen overlay={<WelcomeLayer />} />,
  },
  {
    ...phone,
    name: 'first-phone',
    title: '首页 · 手机 · 第一次：打开 App 就是歪比',
    fixtures: firstFixtures,
    render: () => <PhoneConversation overlay={<><BotConversationHeader current="main" /><WelcomeLayer compact /></>} />,
  },
  {
    ...desktop,
    name: 'ongoing-desktop',
    title: '首页 · 进行中的对话（头像条里歪比是打开状态）',
    fixtures: ongoingFixtures,
    render: () => <DesktopScreen />,
  },
  {
    ...phone,
    name: 'ongoing-phone',
    title: '首页 · 手机 · 进行中的对话',
    fixtures: ongoingFixtures,
    render: () => <PhoneConversation overlay={<BotConversationHeader current="main" />} />,
  },
  {
    ...desktop,
    name: 'running-desktop',
    title: '首页 · 歪比在工作',
    fixtures: runningFixtures,
    render: () => <DesktopScreen />,
  },
  {
    ...phone,
    name: 'running-phone',
    title: '首页 · 手机 · 歪比在工作',
    fixtures: runningFixtures,
    render: () => <PhoneConversation overlay={<BotConversationHeader current="main" />} />,
  },
]);

export default designFrames;
