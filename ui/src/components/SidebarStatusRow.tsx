// The sidebar's bottom row on every surface (ui/design: chrome/bottom-row): the
// status on the left (the running state on the Web, the computer shown in the
// desktop app), the version on the right. One height and one alignment, flush
// with the Apps and Settings row above it.
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { cn } from '@/lib/utils';

export function SidebarStatusRow({ status, version }: { status: ReactNode; version?: ReactNode }) {
  return (
    <div data-sidebar-status-row className="flex h-8 min-w-0 items-center justify-between gap-2">
      <div className="flex min-w-0 flex-1 items-center">{status}</div>
      {version && <div data-version-trigger className="shrink-0">{version}</div>}
    </div>
  );
}

/** The dot that leads every status: running, stopped, or needing attention. */
export function StatusDot({ tone, label }: { tone: 'running' | 'stopped' | 'attention'; label?: string }) {
  return (
    <span
      data-status-indicator
      aria-label={label}
      className={cn(
        'size-2 shrink-0 rounded-full',
        tone === 'running' ? 'bg-mint shadow-glow-dot-mint' : tone === 'attention' ? 'bg-gold' : 'bg-muted',
      )}
    />
  );
}

/** The Web's status: whether this Avibe is running. */
export function RunningStatus({ running }: { running: boolean }) {
  const { t } = useTranslation();
  return (
    <span className="flex min-w-0 items-center gap-2 text-xs text-muted">
      <StatusDot tone={running ? 'running' : 'stopped'} />
      <span className="truncate">{t(running ? 'common.running' : 'common.stopped')}</span>
    </span>
  );
}
