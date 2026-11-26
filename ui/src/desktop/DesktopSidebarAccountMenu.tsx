import { useId, useRef, useState, type ReactElement, type ReactNode } from 'react';
import { Slot } from '@radix-ui/react-slot';
import { ArrowLeftRight, LogOut } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { SidebarStatusRow, StatusDot } from '@/components/SidebarStatusRow';
import { ContextMenuItem } from '@/components/ui/context-menu';
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover';
import type { DesktopClientProjection, DesktopClientView } from './bridge';
import { cn } from '@/lib/utils';

/** ui/design: chrome/sidebar-account-menu. Native owns navigation and sign-out. */
export function DesktopSidebarAccountMenu({ state, trigger, version, onPresent }: {
  state: DesktopClientProjection;
  trigger: ReactElement;
  version: ReactNode;
  onPresent: (view: DesktopClientView) => void;
}) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const menuId = useId();
  const triggerId = useId();
  const select = (view: DesktopClientView) => {
    setOpen(false);
    onPresent(view);
  };
  const { t } = useTranslation();
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

  return <Popover open={open} onOpenChange={setOpen}>
    <PopoverAnchor asChild>
      <div className={cn(open && '[&_[data-account-trigger]]:bg-field-hover')}>
        <SidebarStatusRow status={<Slot ref={triggerRef} id={triggerId}
          aria-haspopup="menu" aria-expanded={open} aria-controls={open ? menuId : undefined}
          onClick={() => setOpen((value) => !value)}>{trigger}</Slot>} version={version} />
      </div>
    </PopoverAnchor>
    <PopoverContent ref={contentRef} id={menuId} aria-labelledby={triggerId}
      side="top" align="start" sideOffset={6} role="menu"
      onOpenAutoFocus={(event) => {
        event.preventDefault();
        contentRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
      }}
      onCloseAutoFocus={(event) => {
        event.preventDefault();
        triggerRef.current?.focus();
      }}
      onInteractOutside={(event) => {
        // The trigger toggles itself; do not close it before its click can run.
        if (event.target instanceof Node && triggerRef.current?.contains(event.target)) event.preventDefault();
      }}
      onKeyDown={(event) => {
        if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'));
        const current = items.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
          : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
        items[next]?.focus();
      }}
      className="w-[var(--radix-popover-trigger-width)] p-1.5">
      {email && <div data-menu-email className="truncate px-2.5 pb-1 pt-1 text-[12px] text-muted">{email}</div>}
      {instance}
      <div className="my-1 border-t border-border" />
      <div data-menu-switch><ContextMenuItem icon={<ArrowLeftRight className="size-4 text-muted" />} label={t('desktopClient.switchInstance')} onClick={() => select('instances')} /></div>
      <div data-menu-sign-out><ContextMenuItem danger icon={<LogOut className="size-4" />} label={t('desktopClient.signOut')} onClick={() => select('sign-out')} /></div>
    </PopoverContent>
  </Popover>;
}
