import { ArrowLeftRight, LogOut } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { StatusDot } from '@/components/SidebarStatusRow';
import { ContextMenuItem } from '@/components/ui/context-menu';
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover';
import { DesktopAccountRowView, type DesktopAccountRowViewProps } from '@/desktop/DesktopAccountRowView';
import { cn } from '@/lib/utils';

const noop = () => {};

/**
 * Proposal (Max, 2026-11-26 09:37): signed in, the sidebar's instance button opens a menu above it,
 * as Claude's account menu does. From the top, the signed-in email, then the instance this window
 * shows, neither of them a control; a line; then Switch instance and Sign out on this device, the
 * latter in the destructive ink. The row itself, the version beside it, Settings and the signed-out
 * row stay as chrome/bottom-row draws them. The menu is as wide as the row and opens upward, clear of
 * the button, which keeps its hover wash while the menu is open. The instance line (Max, 09:58): its
 * dot, name and state, 已连接 while connected (Max, 10:00); offline and while switching it says so instead.
 */
export function DesktopSidebarAccountMenu({ row }: { row: Omit<DesktopAccountRowViewProps, 'onPresent'> }) {
  const { t } = useTranslation();
  const { state } = row;
  const offline = state.connection.status === 'remote_unreachable';
  const busy = state.connection.status === 'switching';
  const name = state.connection.committed.kind === 'local'
    ? state.local.slug ?? t('desktopClient.thisDevice') : state.connection.committed.name;
  // The instance list's dot (InstanceSwitcherView): green while reachable, grey offline, gold while connecting.
  const dot = offline ? <StatusDot tone="stopped" label={t('desktopClient.disconnected')} />
    : busy ? <StatusDot tone="connecting" label={t('desktopClient.connecting')} />
    : <StatusDot tone="running" label={t('desktopClient.connected')} />;
  const email = state.account.identity?.email;

  // The connection's state in words, after the name: the name truncates, never the state.
  const status = t(offline ? 'common.offline' : busy ? 'desktopClient.connecting' : 'devices.reach.connected');

  // The dot centred in the menu icons' 16 px column, the name on their labels' line (Max, 10:01), the
  // state after it.
  const instance = <div data-menu-instance className="flex min-w-0 items-center gap-2.5 px-2.5 py-1.5">
    <span className="grid size-4 shrink-0 place-items-center">{dot}</span>
    <span className="flex min-w-0 items-baseline gap-1.5">
      <span className={cn('min-w-0 truncate text-[12.5px] font-medium', offline ? 'text-muted' : 'text-foreground')}>{name}</span>
      <span data-menu-instance-status className="shrink-0 text-[12px] text-muted">{status}</span>
    </span>
  </div>;

  return <Popover open>
    <PopoverAnchor asChild>
      <div className="[&_[data-account-trigger]]:bg-field-hover">
        <DesktopAccountRowView {...row} onPresent={noop} />
      </div>
    </PopoverAnchor>
    <PopoverContent side="top" align="start" sideOffset={6} role="menu" onOpenAutoFocus={(event) => event.preventDefault()}
      className="w-[var(--radix-popover-trigger-width)] p-1.5">
      {email && <div data-menu-email className="truncate px-2.5 pb-1 pt-1 text-[12px] text-muted">{email}</div>}
      {instance}
      <div className="my-1 border-t border-border" />
      <div data-menu-switch><ContextMenuItem icon={<ArrowLeftRight className="size-4 text-muted" />} label={t('desktopClient.switchInstance')} onClick={noop} /></div>
      <div data-menu-sign-out><ContextMenuItem danger icon={<LogOut className="size-4" />} label={t('desktopClient.signOut')} onClick={noop} /></div>
    </PopoverContent>
  </Popover>;
}
