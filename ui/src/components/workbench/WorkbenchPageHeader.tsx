import type { ReactNode } from 'react';
import clsx from 'clsx';

type Accent = 'mint' | 'cyan' | 'violet' | 'gold';

// Accent-driven icon-box surface. The mint variant is the canonical workbench
// page header (Agents, Skills); cyan/violet/gold are available for siblings.
const ACCENT_BOX: Record<Accent, string> = {
  mint: 'border-mint/40 bg-mint-soft text-mint shadow-[0_0_18px_-6px_rgba(91,255,160,0.5)]',
  cyan: 'border-cyan/40 bg-cyan-soft text-cyan shadow-[0_0_18px_-6px_rgba(63,224,229,0.5)]',
  violet: 'border-violet/40 bg-violet-soft text-violet shadow-[0_0_18px_-6px_rgba(124,91,255,0.5)]',
  gold: 'border-gold/40 bg-gold/[0.12] text-gold shadow-[0_0_18px_-6px_rgba(255,200,87,0.5)]',
};

export interface WorkbenchPageHeaderProps {
  icon: ReactNode;
  title: string;
  subtitle?: string;
  accent?: Accent;
  /** Right-aligned actions (buttons). */
  actions?: ReactNode;
  /** Move a dense action group below the title on narrow mobile screens. */
  stackActionsOnMobile?: boolean;
}

/**
 * Shared workbench page header: a 40px accent icon-box + title + subtitle +
 * optional right-aligned actions. Mirrors design.pen (the Agents / Skills page
 * headers are identical bar the icon + copy). Extracted so capability pages
 * reuse one header instead of re-rolling the markup.
 */
export function WorkbenchPageHeader({
  icon,
  title,
  subtitle,
  accent = 'mint',
  actions,
  stackActionsOnMobile = false,
}: WorkbenchPageHeaderProps) {
  return (
    <div className={clsx('flex items-center gap-4', stackActionsOnMobile && 'flex-wrap')}>
      <div
        className={clsx(
          'flex size-10 shrink-0 items-center justify-center rounded-[10px] border',
          ACCENT_BOX[accent],
        )}
      >
        {icon}
      </div>
      <div className="flex min-w-0 flex-1 flex-col">
        <h1 className="text-[24px] font-bold text-foreground">{title}</h1>
        {subtitle ? <p className="text-[12px] leading-snug text-muted">{subtitle}</p> : null}
      </div>
      {actions ? (
        <div
          className={clsx(
            'flex shrink-0 items-center gap-2',
            stackActionsOnMobile && 'w-full min-w-0 flex-wrap justify-end sm:w-auto sm:flex-nowrap',
          )}
        >
          {actions}
        </div>
      ) : null}
    </div>
  );
}
