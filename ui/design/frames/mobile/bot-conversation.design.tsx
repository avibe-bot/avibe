// Inside a Bot's conversation, on a phone: the shipped chat page,
// with the header's Bot as a switch. Tapping the avatar and name opens every Bot;
// the cyan count on the chevron says other Bots have unread replies. The header and
// the sheet are kit proposals drawn over the shipped page.
import { frames } from '../../frame';
import { BotConversationHeader } from '../../kit/BotConversationHeader';
import { BotSwitchSheet } from '../../kit/BotSwitchSheet';
import { botConversationApi } from '../../shell/fixtures';
import { PhoneConversation } from '../../shell/PhoneScreen';
import { agentsApi, csoTranscript, mainRunningFixtures, mainTranscript, team, teamFixtures } from '../../shell/teamFixtures';

const [main, cso] = team;
// Its own conversation is open, so the security lead's replies are read; the main Bot's one is not.
const inCso = {
  ...teamFixtures,
  unreadBySession: { ses_main: 1, ses_demo_cache: 3 },
  api: { ...agentsApi, ...botConversationApi(cso, team, csoTranscript) },
};
const inMainRunning = { ...mainRunningFixtures, api: { ...agentsApi, ...botConversationApi(main, team, mainTranscript, true) } };

const designFrames = frames([
  {
    name: 'header',
    title: '伙伴对话 · 页头的伙伴可切换，其他伙伴有未读',
    width: 390,
    height: 844,
    status: 'approved',
    fixtures: inCso,
    route: '/chat/ses_cso',
    render: () => <PhoneConversation overlay={<BotConversationHeader current="cso" />} />,
  },
  {
    name: 'switch',
    title: '伙伴对话 · 点名字：切到另一位伙伴',
    width: 390,
    height: 844,
    status: 'approved',
    fixtures: inCso,
    route: '/chat/ses_cso',
    render: () => (
      <PhoneConversation overlay={<><BotConversationHeader current="cso" open /><BotSwitchSheet current="cso" /></>} />
    ),
  },
  {
    name: 'main-running',
    title: '伙伴对话 · 歪比正在工作',
    width: 390,
    height: 844,
    status: 'approved',
    fixtures: inMainRunning,
    route: '/chat/ses_main',
    render: () => <PhoneConversation overlay={<BotConversationHeader current="main" />} />,
  },
  {
    name: 'switch-small',
    title: '伙伴对话 · 切换 · 小屏 375×667',
    width: 375,
    height: 667,
    status: 'approved',
    fixtures: inCso,
    route: '/chat/ses_cso',
    render: () => (
      <PhoneConversation overlay={<><BotConversationHeader current="cso" open /><BotSwitchSheet current="cso" /></>} />
    ),
  },
]);

export default designFrames;
