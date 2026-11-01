import type { ReactNode } from 'react';

/** The desktop sidebar column as AppShell draws it (aside classes, without the bottom cluster). */
export function SidebarColumn({ children }: { children: ReactNode }) {
  return (
    <aside className="flex h-full w-full flex-col justify-between gap-6 border-r border-border bg-[var(--sidebar-background)] px-4 pb-4 pt-[0.625rem]">
      <div className="flex min-h-0 flex-1 flex-col">{children}</div>
    </aside>
  );
}
