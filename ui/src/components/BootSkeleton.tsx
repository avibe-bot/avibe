import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import logoImg from '../assets/logo.png';
import { MobileBrandHeader } from './MobileChrome';
import { Skeleton, SkeletonShimmer } from './ui/skeleton';
import { MobileProjectCardsSkeleton } from './workbench/listSkeletons';

// What the app draws while the route guard re-checks the session on a cold start
// (ui/design: system/skeleton/boot-*): the shell's outline in place of a centered
// "Loading…". The brand is known, so it is drawn as itself; the navigation and the
// lists, which depend on who is signed in, are blocks the size of their rows.

/** A section label of the sidebar (伙伴, 能力, 项目): mono caps, one line. */
const SectionLabel = ({ className }: { className?: string }) => (
  <div className={clsx('flex items-center px-1 font-mono text-[10px]', className)}>
    <span className="flex h-[1lh] items-center">
      <Skeleton className="h-1.5 w-9" />
    </span>
  </div>
);

/** The capability links (`SidebarNavRow`). */
const NavRowsSkeleton = ({ rows }: { rows: number }) => (
  <div className="flex flex-col gap-0.5">
    {Array.from({ length: rows }, (_, index) => (
      <div key={index} className="flex items-center gap-2.5 rounded-lg border border-transparent px-3 py-2 text-[13px]">
        <Skeleton className="size-4 rounded-[4px]" />
        <span className="flex h-[1lh] items-center">
          <Skeleton className={index % 2 ? 'h-2 w-12' : 'h-2 w-16'} />
        </span>
      </div>
    ))}
  </div>
);

const Brand = () => {
  const { t } = useTranslation();
  return (
    <div className="flex min-w-0 items-center gap-2.5">
      <img
        src={logoImg}
        alt=""
        aria-hidden="true"
        className="size-8 shrink-0 rounded-lg border border-mint/35 bg-mint/[0.08] object-cover shadow-glow-sm-mint"
      />
      <div className="min-w-0 leading-tight">
        <div className="truncate text-[13px] font-semibold text-foreground">{t('appShell.title')}</div>
        <div className="truncate text-[11px] text-muted">{t('appShell.subtitle')}</div>
      </div>
    </div>
  );
};

export const BootSkeleton = () => {
  const { t } = useTranslation();
  return (
    <div role="status" aria-busy="true" className="flex h-[var(--app-shell-h)] min-h-0 bg-background text-foreground">
      <span className="sr-only">{t('common.loading')}</span>
      {/* Desktop: the sidebar column as AppShell lays it out, and an empty page beside it. */}
      <aside className="hidden h-full w-[var(--app-sidebar-w)] shrink-0 flex-col border-r border-border bg-[var(--sidebar-background)] px-4 pb-4 pt-[calc(0.625rem+var(--shell-titlebar-inset))] md:flex">
        <div className="flex min-h-0 flex-1 flex-col gap-2.5">
          <div className="flex shrink-0 items-center gap-2 pb-3">
            <Brand />
            <Skeleton className="ml-auto size-8 rounded-lg" />
          </div>
          <SkeletonShimmer className="flex flex-col gap-2.5 rounded-md">
            {/* Bots: their faces and names. */}
            <div className="mb-2.5 flex flex-col gap-1">
              <SectionLabel className="h-4" />
              <div className="flex items-start gap-1">
                {Array.from({ length: 2 }, (_, index) => (
                  <div key={index} className="flex w-14 flex-col items-center gap-1.5 pt-1.5">
                    <Skeleton className="size-10 rounded-full" />
                    <span className="flex h-[1lh] items-center text-[12px]">
                      <Skeleton className="h-1.5 w-8" />
                    </span>
                  </div>
                ))}
              </div>
            </div>
            <div className="flex flex-col gap-1.5">
              <SectionLabel />
              <NavRowsSkeleton rows={4} />
            </div>
            <div className="flex flex-col gap-1.5">
              <SectionLabel className="h-7" />
              <div className="flex flex-col gap-0.5">
                {Array.from({ length: 5 }, (_, index) => (
                  <div key={index} className="flex h-8 items-center gap-1.5 py-1.5 pl-[13px] pr-2">
                    <Skeleton className="size-3.5 rounded-[3px]" />
                    <span className="flex h-[1lh] flex-1 items-center text-[12px]">
                      <Skeleton className={index % 2 ? 'h-2 w-[46%]' : 'h-2 w-[62%]'} />
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </SkeletonShimmer>
        </div>
      </aside>
      <main className="hidden min-w-0 flex-1 md:block" />
      {/* Phone: the shipped brand header, the list's cards, and the tab bar's five places. */}
      <div className="flex min-w-0 flex-1 flex-col md:hidden">
        <MobileBrandHeader />
        <div className="min-h-0 flex-1 px-4 pt-4">
          <MobileProjectCardsSkeleton rows={4} quiet />
        </div>
        <SkeletonShimmer className="fixed inset-x-0 bottom-0 border-t border-border bg-surface/96 px-2 pt-2 pb-[calc(0.5rem+env(safe-area-inset-bottom))]">
          <div className="flex items-end justify-between gap-1">
            {Array.from({ length: 5 }, (_, index) => (
              <div key={index} className="flex min-w-0 flex-1 flex-col items-center justify-center gap-1 px-1 py-2 text-[10px]">
                <span className="flex h-7 items-center justify-center">
                  <Skeleton className="size-4 rounded-[4px]" />
                </span>
                <span className="flex h-[1lh] items-center">
                  <Skeleton className="h-1.5 w-7" />
                </span>
              </div>
            ))}
          </div>
        </SkeletonShimmer>
      </div>
    </div>
  );
};
