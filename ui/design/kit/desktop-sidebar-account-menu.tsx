import { ArrowLeftRight, Globe, Laptop, LogOut } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { StatusDot } from '@/components/SidebarStatusRow';
import { ContextMenuItem } from '@/components/ui/context-menu';
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover';
import { DesktopAccountRowView, type DesktopAccountRowViewProps } from '@/desktop/DesktopAccountRowView';
import { cn } from '@/lib/utils';

/** How the current instance reads in the menu: A, its status dot and name; B, the instance list's row
 *  in small, its icon, dot, name and domain. */
export type AccountMenuInstanceStyle = 'A' | 'B';

const noop = () => {};

/**
 * Proposal (Max, 2026-11-26 09:37): signed in, the sidebar's instance button opens a menu above it,
 * as Claude's account menu does. From the top, the signed-in email, then the instance this window
 * shows, neither of them a control; a line; then Switch instance and Sign out on this device, the
 * latter in the destructive ink. The row itself, the version beside it, Settings and the signed-out
 * row stay as chrome/bottom-row draws them. The menu is as wide as the row and opens upward, clear of
 * the button, which keeps its hover wash while the menu is open.
 */
export function DesktopSidebarAccountMenu({ row, instanceStyle, host }: {
  row: Omit<DesktopAccountRowViewProps, 'onPresent'>;
  instanceStyle: AccountMenuInstanceStyle;
  /** The instance's domain, for style B. Absent: this device's own name for it. */
  host?: string;
}) {
  const { t } = useTranslation();
  const { state } = row;
  const remote = state.connection.committed.kind === 'remote';
  const offline = state.connection.status === 'remote_unreachable';
  const busy = state.connection.status === 'switching';
  const name = state.connection.committed.kind === 'local'
    ? state.local.slug ?? t('desktopClient.thisDevice') : state.connection.committed.name;
  // The instance list's dot (InstanceSwitcherView): green while reachable, grey offline, gold while connecting.
  const dot = offline ? <StatusDot tone="stopped" label={t('desktopClient.disconnected')} />
    : busy ? <StatusDot tone="connecting" label={t('desktopClient.connecting')} />
    : <StatusDot tone="running" label={t('desktopClient.connected')} />;
  const email = state.account.identity?.email;

  const instance = instanceStyle === 'A'
    ? <div data-menu-instance className="flex min-w-0 items-center gap-2.5 px-2.5 py-1.5">
      <span className="grid size-4 shrink-0 place-items-center">{dot}</span>
      <span className={cn('min-w-0 truncate text-[12.5px] font-medium', offline ? 'text-muted' : 'text-foreground')}>{name}</span>
    </div>
    : <div data-menu-instance className="flex min-w-0 items-center gap-2.5 px-2.5 py-1.5">
      <span className={cn('grid size-8 shrink-0 place-items-center rounded-lg', offline ? 'bg-field text-muted' : 'bg-mint-soft text-mint-ink')}>
        {remote ? <Globe className="size-4" /> : <Laptop className="size-4" />}
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex min-w-0 items-center gap-1.5">
          {dot}
          <span className={cn('min-w-0 truncate text-[12.5px] font-medium', offline ? 'text-muted' : 'text-foreground')}>{name}</span>
        </span>
        <span className="mt-0.5 block truncate text-[11px] text-muted">
          {[offline && t('desktopAccount.offline'), host ?? t('desktopClient.thisDevice')].filter(Boolean).join(' · ')}
        </span>
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
