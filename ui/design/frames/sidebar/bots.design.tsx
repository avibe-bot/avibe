// Bots in the sidebar (docs/plans/bots.md D5): the shipped strip, hover card, and Inbox icon,
// drawn by the shipped sidebar from fixtures. The main Bot's mascot has no well, here as
// anywhere (owner, 2026-11-04); the other Bots follow it in the server's order (D13). With
// every place for a custom Bot taken (D12) the strip has no Add: its room goes to the Bots,
// and the Agents page says why adding waits. Holding an avatar with the mouse speaks to that
// Bot (owner decision, 2026-11-04): the composer's ring, then the card beside the avatar; after
// sending the reader stays where they are, and the toast's View opens the conversation.
import type { ReactNode } from 'react';

import { WorkbenchSidebar } from '@/components/workbench/WorkbenchSidebar';

import { frames } from '../../frame';
import { DesktopScreen } from '../../shell/DesktopScreen';
import { HeldBot } from '../../shell/HeldBot';
import { csoFixtures } from '../composer/fixtures';
import { demoBots, demoProjects, inboxRow, mainBot, type WorkbenchFixtures } from '../../shell/fixtures';
import { fullTeam, spoken, team as teamBots } from '../../shell/teamFixtures';
import { SidebarColumn } from '../../shell/SidebarColumn';

const sidebar = () => (
  <SidebarColumn>
    <WorkbenchSidebar />
  </SidebarColumn>
);

const team: WorkbenchFixtures = {
  projects: demoProjects,
  bots: demoBots,
  botStatus: { ses_pm: 'running' },
  unreadBySession: { ses_cso: 2, ses_demo_cache: 3 },
  inboxSessions: [inboxRow('ses_cso', null, '首席安全官', 'avault 依赖里发现 2 个高危漏洞', 20, 2)],
  // The hover card reads the Bot's latest message when it opens.
  api: {
    '/api/inbox?limit=1&session=ses_pm': {
      sessions: [inboxRow('ses_pm', null, '产品经理', '需求池按优先级排好了，前三项需要你拍板。', 2)],
    },
  },
};

// A latest message written in Markdown, long enough to fill a window if the card drew it whole.
const longReply = [
  '## 本周进度',
  '',
  '已经完成了以下工作：',
  '',
  '1. **登录流程**重构，合并了 `auth/session.ts` 里的三处重复逻辑',
  '2. 修复了 [#3195](https://github.com/avibe-bot/avibe/pull/3195) 引入的输入框光标问题',
  '3. 补齐了 WebKit 的浏览器检查，慢点击也覆盖了',
  '',
  '```ts',
  'export const excerpt = (text: string) => text.slice(0, 160);',
  '```',
  '',
  '> 下一步：等你确认发布窗口，我再把变更说明发到群里。',
  '',
  '- 风险：CI 偶发超时，已加重试',
  '- 待办：更新中英文文档',
].join('\n');
const longLatest: WorkbenchFixtures = {
  ...team,
  botStatus: {},
  api: {
    '/api/inbox?limit=1&session=ses_pm': {
      sessions: [inboxRow('ses_pm', null, '产品经理', longReply, 2)],
    },
  },
};

// In the security lead's conversation, holding the PM's avatar.
const recording = { phase: 'recording', text: spoken.text, stash: spoken.stash, seconds: 6 } as const;
const held = (hold: Parameters<typeof HeldBot>[0]['hold']) => () => (
  <HeldBot handle="pm" pointer="mouse" hold={hold}>
    <DesktopScreen />
  </HeldBot>
);
/** After a take to the PM: the PM is the Bot the user last wrote to, right after the main Bot (bots.md D13). */
const [mainOfTeam, csoOfTeam, pmOfTeam, researchOfTeam] = teamBots;
const holdFrame = (name: string, title: string, render: () => ReactNode, fixtures = csoFixtures()) => (
  { name, title, width: 1440, height: 900, status: 'shipped' as const, fixtures, route: '/chat/ses_cso', render }
);

const designFrames = frames([
  {
    name: 'new-user',
    title: '伙伴 · 只有歪比',
    width: 248,
    height: 360,
    status: 'shipped',
    fixtures: { projects: demoProjects, bots: [mainBot] },
    render: sidebar,
  },
  {
    name: 'three',
    title: '伙伴 · 3 个伙伴：未读、运行中',
    width: 248,
    height: 360,
    status: 'shipped',
    fixtures: team,
    render: sidebar,
  },
  {
    name: 'at-limit',
    title: '伙伴 · 刚好 5 位伙伴：没有「添加」，头像横向滑动',
    width: 248,
    height: 360,
    status: 'shipped',
    fixtures: { ...team, bots: fullTeam },
    render: sidebar,
  },
  {
    name: 'main-open',
    title: '伙伴 · 在歪比的对话里：歪比没有底色，选中的细圈画在同样大小的圆周上',
    width: 248,
    height: 360,
    status: 'shipped',
    fixtures: team,
    route: '/chat/ses_main',
    render: sidebar,
  },
  holdFrame('hold-pressing', '按住伙伴说话 · 按下 150ms 后出现圆环，到 500ms 填满（期间移动 >6px = 普通点击）', held({ phase: 'pressing', progress: 0.55 })),
  holdFrame('hold-recording', '按住伙伴说话 · 录音中：卡片在头像右边，松开发送 · 移开头像或 Esc 取消', held(recording)),
  holdFrame('hold-armed', '按住伙伴说话 · 指针移开头像：松开取消', held({ ...recording, phase: 'armed', seconds: 7 })),
  holdFrame(
    'hold-sent',
    '按住伙伴说话 · 已发给产品经理：留在原页面，它排到歪比后面',
    held({ outcome: 'sent', text: spoken.final }),
    { ...csoFixtures(), bots: [mainOfTeam, pmOfTeam, csoOfTeam, researchOfTeam] },
  ),
  {
    name: 'hover-card',
    title: '伙伴 · 悬停卡片',
    width: 520,
    height: 360,
    status: 'shipped',
    fixtures: team,
    hover: '[data-bot-handle="pm"]',
    render: () => (
      <div className="h-full w-[248px]">
        {sidebar()}
      </div>
    ),
  },
  {
    name: 'hover-card-long',
    title: '伙伴 · 悬停卡片 · 最近一条是长 Markdown：纯文本摘要，最多两行',
    width: 520,
    height: 360,
    status: 'shipped',
    fixtures: longLatest,
    hover: '[data-bot-handle="pm"]',
    render: () => (
      <div className="h-full w-[248px]">
        {sidebar()}
      </div>
    ),
  },
]);

export default designFrames;
