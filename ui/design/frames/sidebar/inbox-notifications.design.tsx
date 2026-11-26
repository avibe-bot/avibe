import { Popover, PopoverAnchor } from '@/components/ui/popover';
import { InboxHoverPopover } from '@/components/workbench/InboxHoverPopover';

import { frames } from '../../frame';
import { mainBot, type WorkbenchFixtures } from '../../shell/fixtures';

// ui/design: sidebar/inbox-notifications/* — the actual Back to Inbox notification view.
const states: {
  name: string;
  title: string;
  preference: NonNullable<WorkbenchFixtures['desktopNotifications']>;
}[] = [
  { name: 'off', title: '本设备通知 · 已关闭', preference: { enabled: false, authorization: 'authorized' } },
  { name: 'on', title: '本设备通知 · 已开启', preference: { enabled: true, authorization: 'authorized' } },
  { name: 'denied', title: '本设备通知 · 系统权限拒绝', preference: { enabled: true, authorization: 'denied' } },
  { name: 'save-failed', title: '本设备通知 · 保存失败', preference: { enabled: false, authorization: 'authorized', failSave: true } },
];

const designFrames = frames(states.map(({ name, title, preference }) => ({
  name,
  title,
  width: 420,
  height: 480,
  status: 'shipped',
  themes: ['light', 'dark'],
  fixtures: { bots: [mainBot], desktopShell: true, desktopNotifications: preference },
  click: [
    'role=button[name="{t:workbench.notifications.open}"]',
    ...(preference.failSave ? ['role=button[name="{t:workbench.inbox.notifications.enable}"]'] : []),
  ],
  render: () => (
    <Popover open>
      <PopoverAnchor className="absolute left-0 top-4 size-px" />
      <InboxHoverPopover
        sessions={[]}
        unreadBySession={{}}
        unreadSessions={0}
        totalUnread={0}
        canMarkRead
        onItemClick={() => {}}
        onMarkAllRead={() => {}}
        onMouseEnter={() => {}}
        onMouseLeave={() => {}}
      />
    </Popover>
  ),
})));

export default designFrames;
