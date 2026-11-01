// The shipped Inbox hover popover (a portaled overlay), opened next to the shipped sidebar.
import { Popover, PopoverAnchor } from '@/components/ui/popover';
import { InboxHoverPopover } from '@/components/workbench/WorkbenchSidebar';

import { frames } from '../../frame';
import { inboxRow } from '../../shell/fixtures';

const rows = [
  inboxRow('ses_demo_cache', 'avibe', '修复中途断缓存', '定位到清理旧工具结果时前缀断开，PR 已开。', 5, 3),
  inboxRow('ses_demo_pr', 'avibe', 'PR #3073 交付', 'CI 失败：unit-tests (2/8)。', 30, 2),
  inboxRow('ses_demo_audit', 'chainbot', '合约审计', '三个函数的权限检查已补齐，测试全绿。', 90),
];

const designFrames = frames([
  {
    name: 'open',
    title: '收件箱浮层 · 现状',
    width: 420,
    height: 460,
    status: 'shipped',
    render: () => (
      <Popover open>
        <PopoverAnchor className="absolute left-0 top-4 size-px" />
        <InboxHoverPopover
          sessions={rows}
          unreadBySession={{ ses_demo_cache: 3, ses_demo_pr: 2 }}
          unreadSessions={2}
          totalUnread={5}
          canMarkRead
          onItemClick={() => {}}
          onMarkAllRead={() => {}}
          onMouseEnter={() => {}}
          onMouseLeave={() => {}}
        />
      </Popover>
    ),
  },
]);

export default designFrames;
