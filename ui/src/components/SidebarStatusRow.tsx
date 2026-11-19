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

/** The status's leading glyph, in a fixed 10px slot so the label beside it never shifts when the
 *  glyph changes (a dot while connected, a 10px icon while offline or switching). The glyph starts
 *  at the slot's left edge. The row is flush with the Apps button above; a status drawn as a button
 *  (the desktop account trigger) keeps its own inset so the glyph stays inside its hover wash. */
export function StatusGlyph({ children }: { children: ReactNode }) {
  return <span className="flex size-2.5 shrink-0 items-center justify-start [&>svg]:size-2.5">{children}</span>;
}

/** The dot that leads a status, shared with the App instance list. */
export function StatusDot({ tone, label }: { tone: 'running' | 'stopped' | 'connecting'; label?: string }) {
  return (
    <span
      data-status-indicator
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      className={cn('size-2 shrink-0 rounded-full', tone === 'running' ? 'bg-mint shadow-glow-dot-mint' : tone === 'connecting' ? 'bg-gold shadow-glow-dot-gold' : 'bg-muted')}
    />
  );
}

/** The Web's status: whether this Avibe is running. */
export function RunningStatus({ running }: { running: boolean }) {
  const { t } = useTranslation();
  return (
    <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted">
      <StatusGlyph><StatusDot tone={running ? 'running' : 'stopped'} /></StatusGlyph>
      <span className="truncate">{t(running ? 'common.running' : 'common.stopped')}</span>
    </span>
  );
}
