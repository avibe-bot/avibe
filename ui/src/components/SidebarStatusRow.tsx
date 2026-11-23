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

const IDEOGRAPHIC = /^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u;

/** Centres a glyph on the first line of the text beside it, where that line's letters sit rather
 *  than the middle of its line box: halfway up the x-height when the text starts in Latin script,
 *  whose lowercase carries the line, and halfway up the cap height when it starts with an ideograph,
 *  which fills it. The slot takes the text's own font and line height (pass the text's type classes
 *  when the slot does not inherit them) and starts level with it, so the two lines share a baseline
 *  at any size or weight, and a wrapped name keeps the glyph on its first line. */
export function LineGlyph({ text, className, children }: { text: string; className?: string; children: ReactNode }) {
  return (
    <span data-line-glyph className={cn('block shrink-0', className)}>
      <span className={cn('relative inline-block h-0 w-full', IDEOGRAPHIC.test(text.trimStart()) ? 'align-cap-middle' : 'align-middle')}>
        <span className="absolute left-0 top-0 flex -translate-y-1/2">{children}</span>
      </span>
    </span>
  );
}

/** The status's leading glyph, in a fixed 10px slot so the label beside it never shifts when the
 *  glyph changes (a dot while connected, a 10px icon while offline or switching), centred on the
 *  label's letters. The glyph starts at the slot's left edge. The row is flush with the Apps button
 *  above. The desktop account trigger centres its own glyph on the button instead
 *  (`DesktopAccountRowView`). */
export function StatusGlyph({ label, children }: { label: string; children: ReactNode }) {
  return <LineGlyph text={label} className="w-2.5 [&_svg]:size-2.5">{children}</LineGlyph>;
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
  const label = t(running ? 'common.running' : 'common.stopped');
  return (
    <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted">
      <StatusGlyph label={label}><StatusDot tone={running ? 'running' : 'stopped'} /></StatusGlyph>
      <span className="truncate">{label}</span>
    </span>
  );
}
