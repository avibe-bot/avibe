// Starting a Session in a chosen project, now that home has no project picker: the
// sidebar project row's ＋ (shipped, on hover), the 工作台 tab's project row ⋯ →
// 新建会话 on a phone (see mobile/workbench-tab/new-session), or asking the main Bot.
import { frames } from '../../frame';
import { DesktopScreen } from '../../shell/DesktopScreen';
import { botConversationApi, type WorkbenchFixtures } from '../../shell/fixtures';
import { agentsApi, mainTranscript, team, teamFixtures } from '../../shell/teamFixtures';

const fixtures: WorkbenchFixtures = {
  ...teamFixtures,
  unreadBySession: { ses_cso: 2, ses_demo_cache: 3 },
  api: { ...agentsApi, ...botConversationApi(team[0], team, mainTranscript) },
};

const designFrames = frames([
  {
    name: 'sidebar-row',
    title: '在项目里新建会话 · 桌面：项目行悬停出现 ＋',
    width: 1440,
    height: 900,
    status: 'proposed',
    fixtures,
    route: '/chat/ses_main',
    hover: 'aside >> text=chainbot',
    render: () => <DesktopScreen />,
  },
]);

export default designFrames;
