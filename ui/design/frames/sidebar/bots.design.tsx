// Bots sidebar (docs/plans/bots.md D5): the top of the sidebar, drawn with kit components.
import { frames } from '../../frame';
import { BotHoverCard, BotStrip, BrandRowWithInbox, type BotItem } from '../../kit/BotStrip';
import { SidebarColumn } from '../../shell/SidebarColumn';

const vibey: BotItem = {
  handle: 'main',
  name: '歪比',
  role: '你的主助理，帮你安排一切',
  avatar: { kind: 'vibey' },
  preview: '周报草稿已发到飞书，等你确认',
  time: '16:02',
};
const cso: BotItem = {
  handle: 'cso',
  name: '首席安全官',
  role: '依赖与权限的安全审计，每周汇报',
  avatar: { kind: 'emoji', emoji: '🛡️', well: 'cyan' },
  unread: 2,
  preview: 'avault 依赖里发现 2 个高危漏洞',
  time: '15:40',
};
const pm: BotItem = {
  handle: 'pm',
  name: '产品经理',
  role: '需求池整理与优先级',
  avatar: { kind: 'emoji', emoji: '🧭', well: 'gold' },
  running: true,
  preview: '正在整理本周需求池…',
  time: '15:58',
};

const top = (bots: BotItem[], inboxUnread = 0) => () => (
  <SidebarColumn>
    <div className="flex flex-col gap-2.5">
      <BrandRowWithInbox inboxUnread={inboxUnread} />
      <BotStrip bots={bots} />
    </div>
  </SidebarColumn>
);

const designFrames = frames([
  { name: 'new-user', title: '伙伴 · 只有歪比', width: 248, height: 190, status: 'approved', render: top([vibey]) },
  { name: 'three', title: '伙伴 · 3 个伙伴', width: 248, height: 190, status: 'approved', render: top([vibey, cso, pm], 5) },
  {
    name: 'hover-card',
    title: '伙伴 · 悬停卡片',
    width: 280,
    height: 300,
    status: 'approved',
    render: () => (
      <div className="p-4">
        <BotHoverCard bot={pm} />
      </div>
    ),
  },
]);

export default designFrames;
