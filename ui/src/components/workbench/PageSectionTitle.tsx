import type { ReactNode } from 'react';

/** A section's title on a phone's tab page (the Workbench tab's Bots and Projects): one
 *  size, weight, color, and inset for every section below the page header, and a
 *  row height that fits one small icon action. */
export function PageSectionTitle({ title, action }: { title: string; action?: ReactNode }) {
  return (
    <div className="flex h-8 items-center justify-between gap-2 px-1">
      <h2 className="min-w-0 truncate text-[13px] font-semibold text-muted">{title}</h2>
      {action}
    </div>
  );
}
