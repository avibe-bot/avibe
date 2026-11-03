// Inside a Bot's conversation, on a phone: the shipped chat page, whose header's Bot
// is a switch. Tapping the avatar and name opens every Bot; the cyan count on the
// chevron says other Bots have unread replies.
import { frames } from '../../frame';
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
const openSwitch = ['[data-chat-header] button[aria-haspopup="dialog"]'];

const designFrames = frames([
  { name: 'header', title: '伙伴对话 · 页头的伙伴可切换，其他伙伴有未读', width: 390, height: 844, status: 'shipped', fixtures: inCso, route: '/chat/ses_cso', render: () => <PhoneConversation /> },
  { name: 'switch', title: '伙伴对话 · 点名字：切到另一位伙伴', width: 390, height: 844, status: 'shipped', fixtures: inCso, route: '/chat/ses_cso', click: openSwitch, render: () => <PhoneConversation /> },
  { name: 'main-running', title: '伙伴对话 · 歪比正在工作', width: 390, height: 844, status: 'shipped', fixtures: inMainRunning, route: '/chat/ses_main', render: () => <PhoneConversation /> },
  { name: 'switch-small', title: '伙伴对话 · 切换 · 小屏 375×667', width: 375, height: 667, status: 'shipped', fixtures: inCso, route: '/chat/ses_cso', click: openSwitch, render: () => <PhoneConversation /> },
]);

export default designFrames;
