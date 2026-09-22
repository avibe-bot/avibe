import type { HTMLAttributes, ReactNode } from 'react';
import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  ChevronRight,
  Download,
  FileSearch,
  FileText,
  Folder,
  HardDrive,
  Home,
  Monitor,
  RefreshCw,
  Search,
  X,
  type LucideIcon,
} from 'lucide-react';
import clsx from 'clsx';

import type { Favorite, FsEntry } from '../../lib/filesApi';
import { Button } from './button';
import { MobileAppHeader } from '../apps/MobileAppHeader';

export type FileBrowserRow = {
  entry: FsEntry;
  full: string;
  dir: string;
  rel?: string;
  matchCount?: number;
};

export type FileBrowserProject = {
  label: string;
  path: string;
};

export type FileBrowserDropProps = {
  onDragOver: (event: React.DragEvent) => void;
  onDragLeave: () => void;
  onDrop: (event: React.DragEvent) => void;
};

export interface FileBrowserProps {
  fullBleed?: boolean;
  mobileRoute?: boolean;
  mobileTitle?: string;
  title?: ReactNode;
  tagline?: ReactNode;
  cwd: string;
  crumbs: { label: string; path: string }[];
  sysFavs: Favorite[];
  projectFavs: FileBrowserProject[];
  loading?: boolean;
  searchBusy?: boolean;
  query?: string;
  onQueryChange?: (value: string) => void;
  searchMode?: 'name' | 'content';
  onSearchModeChange?: (mode: 'name' | 'content') => void;
  onRefresh?: () => void;
  onNavigate: (path: string) => void;
  onFavoriteNavigate?: (path: string) => void;
  onClearQuery?: () => void;
  navigationControl?: ReactNode;
  showHidden: boolean;
  onShowHiddenChange: (showHidden: boolean) => void;
  error?: string | null;
  toolbarActions?: ReactNode;
  listHeader?: ReactNode;
  listContent: ReactNode;
  paneProps?: HTMLAttributes<HTMLDivElement>;
  listProps?: HTMLAttributes<HTMLDivElement>;
  dropOverlay?: ReactNode;
  footerContent?: ReactNode;
  statusContent?: ReactNode;
  dropTarget?: string | null;
  getDropProps?: (path: string) => FileBrowserDropProps;
  className?: string;
}

const FAV_ICON: Record<string, LucideIcon> = {
  home: Home,
  desktop: Monitor,
  downloads: Download,
  documents: FileText,
  root: HardDrive,
};

export const FileBrowser: React.FC<FileBrowserProps> = ({
  fullBleed = false,
  mobileRoute = false,
  mobileTitle,
  title,
  tagline,
  cwd,
  crumbs,
  sysFavs,
  projectFavs,
  loading = false,
  searchBusy = false,
  query,
  onQueryChange,
  searchMode = 'name',
  onSearchModeChange,
  onRefresh,
  onNavigate,
  onFavoriteNavigate,
  onClearQuery,
  navigationControl,
  showHidden,
  onShowHiddenChange,
  error,
  toolbarActions,
  listHeader,
  listContent,
  paneProps,
  listProps,
  dropOverlay,
  footerContent,
  statusContent,
  dropTarget,
  getDropProps,
  className,
}) => {
  const { t } = useTranslation();
  const hasSearch = query !== undefined && onQueryChange;
  const navigateTo = (path: string) => {
    onClearQuery?.();
    onNavigate(path);
  };
  const navigateFavoriteTo = (path: string) => {
    onClearQuery?.();
    (onFavoriteNavigate || onNavigate)(path);
  };
  const railDropProps = (path: string) => getDropProps?.(path);

  return (
    <div
      className={clsx(
        fullBleed
          ? 'relative flex h-full w-full flex-col bg-surface pb-[env(safe-area-inset-bottom)]'
          : 'relative flex h-[calc(100dvh-7rem)] min-h-[460px] flex-col gap-3 md:h-[calc(100vh-8rem)]',
        className,
      )}
    >
      {mobileRoute && mobileTitle && <MobileAppHeader title={mobileTitle} icon={Folder} />}
      {!fullBleed && (
        <div>
          {title && <h1 className="text-[18px] font-semibold text-foreground">{title}</h1>}
          {tagline && <p className="text-[12px] text-muted">{tagline}</p>}
        </div>
      )}

      <div className={clsx('flex min-h-0 flex-1 flex-col overflow-hidden', !fullBleed && 'rounded-xl border border-border')}>
        <div
          className={clsx(
            'flex items-center gap-2 border-b border-border bg-surface-2/60',
            mobileRoute ? 'flex-wrap px-2 py-2' : 'px-3 py-2',
          )}
        >
          <div className={clsx('flex min-w-0 items-center gap-0.5 overflow-x-auto', mobileRoute ? 'order-1 w-full' : 'flex-1')}>
            {onRefresh && (
              <Button
                type="button"
                size="icon"
                variant="ghost"
                className="size-7 shrink-0 text-muted"
                aria-label={t('apps.fileBrowser.refresh')}
                onClick={onRefresh}
              >
                <RefreshCw className={clsx('size-3.5', (loading || searchBusy) && 'animate-spin')} />
              </Button>
            )}
            {crumbs.map((crumb, index) => (
              <span key={crumb.path} className="flex shrink-0 items-center">
                {index > 0 && <ChevronRight className="size-3 shrink-0 text-muted" />}
                <button
                  type="button"
                  onClick={() => navigateTo(crumb.path)}
                  {...railDropProps(crumb.path)}
                  className={clsx(
                    'max-w-[140px] truncate rounded px-1.5 py-0.5 text-[12.5px] text-muted transition hover:bg-foreground/[0.06] hover:text-foreground',
                    dropTarget === crumb.path && 'bg-cyan-soft text-foreground ring-1 ring-inset ring-cyan',
                  )}
                >
                  {crumb.label}
                </button>
              </span>
            ))}
          </div>

          {navigationControl && (mobileRoute ? <div className="order-2 w-full">{navigationControl}</div> : navigationControl)}

          {hasSearch && (
            <label
              className={clsx(
                'flex items-center gap-1.5 rounded-lg border border-border bg-surface px-2 py-1',
                mobileRoute ? 'order-2 w-full' : 'shrink-0',
              )}
            >
              {searchBusy ? <RefreshCw className="size-3.5 shrink-0 animate-spin text-muted" /> : <Search className="size-3.5 shrink-0 text-muted" />}
              <input
                value={query}
                onChange={(event) => onQueryChange(event.target.value)}
                placeholder={t(searchMode === 'content' ? 'apps.fileBrowser.searchContentPlaceholder' : 'apps.fileBrowser.searchPlaceholder')}
                className={clsx(
                  'min-w-0 flex-1 bg-transparent text-[12px] text-foreground placeholder:text-muted focus:outline-none',
                  !mobileRoute && 'w-28 flex-none',
                )}
              />
              {onSearchModeChange && (
                <button
                  type="button"
                  aria-pressed={searchMode === 'content'}
                  aria-label={t('apps.fileBrowser.searchContents')}
                  title={t('apps.fileBrowser.searchContents')}
                  onClick={() => onSearchModeChange(searchMode === 'content' ? 'name' : 'content')}
                  className={clsx(
                    'grid size-5 shrink-0 place-items-center rounded transition',
                    searchMode === 'content' ? 'bg-cyan-soft text-cyan-ink' : 'text-muted hover:bg-foreground/10 hover:text-foreground',
                  )}
                >
                  <FileSearch className="size-3.5" />
                </button>
              )}
              {query && (
                <button type="button" onClick={() => onQueryChange('')} className="shrink-0 text-muted transition hover:text-foreground" aria-label={t('common.close')}>
                  <X className="size-3" strokeWidth={2.5} />
                </button>
              )}
            </label>
          )}

          {toolbarActions && (
            <div className={clsx('flex shrink-0 items-center gap-1 overflow-x-auto', mobileRoute ? 'order-3 w-full justify-end' : 'ml-auto')}>
              {toolbarActions}
            </div>
          )}
        </div>

        {(sysFavs.length > 0 || projectFavs.length > 0) && (
          <div className="flex shrink-0 items-center gap-2 overflow-x-auto border-b border-border bg-surface-2/60 px-3 py-2 md:hidden">
            {sysFavs.map((favorite) => {
              const Icon = FAV_ICON[favorite.key] ?? Folder;
              const active = cwd === favorite.path;
              return (
                <button
                  key={favorite.path}
                  type="button"
                  aria-current={active ? 'true' : undefined}
                  onClick={() => navigateFavoriteTo(favorite.path)}
                  className={clsx(
                    'flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-[12.5px] font-medium transition',
                    active ? 'border-cyan/40 bg-cyan-soft text-foreground' : 'border-border-strong text-muted',
                  )}
                >
                  <Icon className="size-3.5 shrink-0" />
                  <span className="max-w-[140px] truncate">{favorite.path.split(/[\\/]/).filter(Boolean).pop() || favorite.path}</span>
                </button>
              );
            })}
            {projectFavs.map((project) => {
              const active = cwd === project.path;
              return (
                <button
                  key={project.path}
                  type="button"
                  aria-current={active ? 'true' : undefined}
                  onClick={() => navigateFavoriteTo(project.path)}
                  className={clsx(
                    'flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-[12.5px] font-medium transition',
                    active ? 'border-cyan/40 bg-cyan-soft text-foreground' : 'border-border-strong text-muted',
                  )}
                >
                  <Folder className={clsx('size-3.5 shrink-0', active ? 'text-cyan-ink' : 'text-cyan-ink/70')} />
                  <span className="max-w-[140px] truncate">{project.label}</span>
                </button>
              );
            })}
          </div>
        )}

        {error && <div className="border-b border-destructive/40 bg-destructive/[0.06] px-3 py-1.5 text-[11.5px] text-destructive-ink">{error}</div>}

        <div className="flex min-h-0 flex-1 overflow-hidden">
          <aside className="hidden w-[196px] shrink-0 flex-col gap-0.5 overflow-y-auto border-r border-border bg-surface-2/40 p-2 md:flex">
            {sysFavs.length > 0 && <RailTitle>{t('apps.fileBrowser.favorites')}</RailTitle>}
            {sysFavs.map((favorite) => {
              const Icon = FAV_ICON[favorite.key] ?? Folder;
              return (
                <RailRow
                  key={favorite.path}
                  icon={<Icon className="size-3.5 text-muted" />}
                  label={favorite.path.split(/[\\/]/).filter(Boolean).pop() || favorite.path}
                  active={cwd === favorite.path}
                  dropActive={dropTarget === favorite.path}
                  dropProps={railDropProps(favorite.path)}
                  onClick={() => navigateFavoriteTo(favorite.path)}
                />
              );
            })}
            {projectFavs.length > 0 && <RailTitle>{t('apps.fileBrowser.projects')}</RailTitle>}
            {projectFavs.map((project) => (
              <RailRow
                key={project.path}
                icon={<Folder className="size-3.5 text-cyan-ink" />}
                label={project.label}
                active={cwd === project.path}
                dropActive={dropTarget === project.path}
                dropProps={railDropProps(project.path)}
                onClick={() => navigateFavoriteTo(project.path)}
              />
            ))}
          </aside>

          <div {...paneProps} className={clsx('relative flex min-w-0 flex-1 flex-col', paneProps?.className)}>
            {listHeader}
            <div className="min-h-0 flex-1 overflow-y-auto py-1" {...listProps}>
              {listContent}
            </div>
            {dropOverlay}
          </div>
        </div>

        {footerContent}

        <div className="flex items-center gap-3 border-t border-border bg-surface-2/60 px-3 py-1.5 text-[11px] text-muted">
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={showHidden} onChange={(event) => onShowHiddenChange(event.target.checked)} className="size-3" />
            {t('apps.fileBrowser.showHidden')}
          </label>
          {statusContent}
        </div>
      </div>
    </div>
  );
};

const RailTitle: React.FC<{ children: ReactNode }> = ({ children }) => (
  <div className="px-1 pb-0.5 pt-1.5 font-mono text-[9px] font-bold uppercase tracking-[0.16em] text-muted">{children}</div>
);

const RailRow: React.FC<{
  icon: ReactNode;
  label: string;
  active: boolean;
  onClick: () => void;
  dropActive?: boolean;
  dropProps?: FileBrowserDropProps;
}> = ({ icon, label, active, onClick, dropActive, dropProps }) => (
  <button
    type="button"
    onClick={onClick}
    {...dropProps}
    className={clsx(
      'flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12.5px] transition',
      dropActive
        ? 'bg-cyan-soft text-foreground ring-1 ring-inset ring-cyan'
        : active
          ? 'bg-cyan-soft text-foreground'
          : 'text-muted hover:bg-foreground/[0.04] hover:text-foreground',
    )}
  >
    <span className="shrink-0">{icon}</span>
    <span className="truncate">{label}</span>
  </button>
);
