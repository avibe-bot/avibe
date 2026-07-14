import { useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { LayoutGrid, Minus, MoreHorizontal, PinOff, Settings, Sun, UserRound, X } from 'lucide-react';

import { APP_REGISTRY, type AppDefinition, type AppId } from '../../apps/registry';
import { showPageAvatar, showPageIconUrl } from '../../apps/showPageAvatar';
import { ShowPageAvatarContent } from '../../apps/showPageAvatarTile';
import { dockIdToSession, useDock } from '../../context/DockContext';
import { useAuthAccount } from '../../lib/useAuthAccount';
import { useShowPageInventory } from '../useShowPages';
import { MoreAccountSection, MoreAppearanceSection, MoreConnectionSection } from '../workbench/MorePage';
import { mobileRouteForDockId } from './mobileDock';

// The mobile Dock — a bottom drawer summoned from the Apps tab (§7.1b, Option B).
// It is the mobile projection of the desktop Dock: the SAME server-side docked
// tiles in the SAME order (pin anywhere → appears everywhere). One tap opens an
// app full-screen (built-ins via their /apps/* route, pinned Show Pages via the
// /apps/show/:sessionId route — no window layer on mobile); a long-press manages
// the tile (取消固定 for every tile, 移出 additionally for AI pages). The former
// More-page content compresses into the footer chip row. Mobile-only: the whole
// surface is `md:hidden` and mounted only in the workbench shell.

type ResidentTile =
  | { kind: 'builtin'; id: string; def: AppDefinition }
  | { kind: 'showpage'; id: string; sessionId: string; title: string; iconVersion: string | null };

type FooterSheet = 'account' | 'appearance' | 'more';

export const MobileDockDrawer: React.FC<{ open: boolean; onClose: () => void }> = ({ open, onClose }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { order, pins, undock, unpin } = useDock();
  const { email } = useAuthAccount();
  const { pages } = useShowPageInventory(open);

  // Long-press manage sheet (touch): a bottom action sheet keyed to a tile. Tap
  // opens the app; a press-hold opens the sheet. A cursor-positioned ContextMenu
  // isn't used here — its close backdrop is z-40, below this z-50 drawer, so taps
  // outside the menu would fall through to the tiles instead of dismissing it.
  const [menu, setMenu] = useState<{ item: ResidentTile; label: string } | null>(null);
  // Which footer overflow sheet is open (账号 / 外观 / 更多) — one reusable shell.
  const [sheet, setSheet] = useState<FooterSheet | null>(null);

  // Long-press detection: a press-hold opens the manage menu and suppresses the
  // click that would otherwise fire on release; a plain tap falls through to click.
  const timerRef = useRef<number | null>(null);
  const suppressClickRef = useRef(false);
  const clearTimer = () => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };

  const pinBySession = useMemo(() => new Map(pins.map((pin) => [pin.session_id, pin])), [pins]);
  const pageBySession = useMemo(() => new Map(pages.map((page) => [page.session_id, page])), [pages]);

  // Reset transient sub-surfaces while closed so a reopen starts clean — the
  // codebase's effect-free "adjust state during render" pattern (cf. LibraryApp's
  // navKey sync), guarded so it can't loop.
  if (!open) {
    if (menu) setMenu(null);
    if (sheet) setSheet(null);
    return null;
  }

  // Resolve a persisted Dock id to a renderable tile: a built-in app (icon +
  // accent from the registry) or a pinned Show Page (letter avatar). Mirrors the
  // desktop Dock's resolveItem; an unknown id is dropped.
  const resolveTile = (id: string): ResidentTile | null => {
    const sessionId = dockIdToSession(id);
    if (sessionId !== null) {
      const page = pageBySession.get(sessionId);
      const title = page
        ? page.title?.trim() || t('chat.untitled')
        : pinBySession.get(sessionId)?.title_snapshot?.trim() || sessionId;
      return { kind: 'showpage', id, sessionId, title, iconVersion: page?.icon_version ?? null };
    }
    const def = APP_REGISTRY[id as AppId];
    return def ? { kind: 'builtin', id, def } : null;
  };

  const openTile = (item: ResidentTile) => {
    navigate(mobileRouteForDockId(item.id));
    onClose();
  };

  const onTilePointerDown = (item: ResidentTile, label: string) => {
    clearTimer();
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      suppressClickRef.current = true;
      setMenu({ item, label });
    }, 480);
  };

  const onTileClick = (item: ResidentTile) => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return; // this click was the release of a long-press that opened the menu
    }
    openTile(item);
  };

  const chipClass =
    'flex min-w-0 flex-1 items-center justify-center gap-1.5 rounded-xl border border-border bg-surface-2/40 px-2.5 py-2.5 text-[12px] font-medium text-muted transition-colors hover:text-foreground active:bg-foreground/[0.05]';

  const sheetTitle: Record<FooterSheet, string> = {
    account: t('more.account'),
    appearance: t('more.appearance'),
    more: t('nav.more'),
  };

  return (
    <div className="md:hidden" role="dialog" aria-modal="true" aria-label={t('nav.apps')}>
      {/* Scrim from the top of the screen down to the tab bar — the tab bar stays
          visible + tappable below it (tapping Apps again toggles the drawer shut). */}
      <button
        type="button"
        aria-label={t('common.close')}
        onClick={onClose}
        className="fixed inset-x-0 top-0 bottom-[calc(4.75rem+env(safe-area-inset-bottom))] z-40 bg-background/70 backdrop-blur-sm"
      />

      {/* The drawer sheet: sits directly above the bottom tab bar. */}
      <div className="fixed inset-x-0 bottom-[calc(4.75rem+env(safe-area-inset-bottom))] z-50 max-h-[72dvh] overflow-y-auto rounded-t-3xl border border-border bg-surface px-4 pb-4 pt-2 shadow-[0_-16px_40px_-12px_rgba(0,0,0,0.55)]">
        {/* Grabber. */}
        <div className="mx-auto mb-3 h-1 w-10 shrink-0 rounded-full bg-border" />

        {/* Header: title + "synced with desktop Dock" caption. */}
        <div className="mb-3 flex items-baseline justify-between gap-3">
          <h2 className="text-[17px] font-bold text-foreground">{t('nav.apps')}</h2>
          <span className="truncate text-[11.5px] text-muted">{t('apps.dock.syncedCaption')}</span>
        </div>

        {/* Tile grid — the docked set, in server order. */}
        {order.length === 0 ? (
          <button
            type="button"
            onClick={() => {
              navigate('/apps/library');
              onClose();
            }}
            className="flex w-full items-center justify-center gap-2 rounded-xl border border-dashed border-border px-3 py-6 text-[12.5px] font-medium text-muted transition-colors hover:border-cyan/60 hover:text-foreground"
          >
            <LayoutGrid className="size-4 shrink-0 text-cyan" />
            <span>{t('apps.dock.emptyHint')}</span>
          </button>
        ) : (
          <div className="grid grid-cols-4 gap-x-2 gap-y-4">
            {order.map((id) => {
              const item = resolveTile(id);
              if (!item) return null;
              const label = item.kind === 'builtin' ? t(item.def.titleKey) : item.title;
              const avatar = item.kind === 'showpage' ? showPageAvatar(item.sessionId, item.title) : null;
              const Icon = item.kind === 'builtin' ? item.def.icon : null;
              const accentVar = item.kind === 'builtin' ? item.def.accent : avatar!.accentVar;
              return (
                <button
                  key={id}
                  type="button"
                  aria-label={label}
                  onClick={() => onTileClick(item)}
                  onPointerDown={() => onTilePointerDown(item, label)}
                  onPointerUp={clearTimer}
                  onPointerMove={clearTimer}
                  onPointerLeave={clearTimer}
                  onPointerCancel={clearTimer}
                  onContextMenu={(e) => e.preventDefault()}
                  style={{ touchAction: 'manipulation' }}
                  className="flex select-none flex-col items-center gap-1.5"
                >
                  <span
                    className="grid size-14 place-items-center overflow-hidden rounded-2xl border text-[20px] font-bold leading-none"
                    style={{
                      color: `var(${accentVar})`,
                      backgroundColor: `color-mix(in srgb, var(${accentVar}) ${item.kind === 'builtin' ? 14 : 16}%, transparent)`,
                      borderColor: `color-mix(in srgb, var(${accentVar}) ${item.kind === 'builtin' ? 30 : 34}%, transparent)`,
                    }}
                  >
                    {Icon ? (
                      <Icon className="size-6" />
                    ) : (
                      <ShowPageAvatarContent
                        iconUrl={item.kind === 'showpage' ? showPageIconUrl(item.sessionId, item.iconVersion) : null}
                        letter={avatar!.letter}
                      />
                    )}
                  </span>
                  <span className="max-w-full truncate text-[11px] font-medium text-muted">{label}</span>
                </button>
              );
            })}
          </div>
        )}

        {/* Library is undockable (§7.1c); when it isn't docked but other tiles are,
            keep a route back to it here. The drawer is the only mobile Apps surface,
            so it must never strand the user with no way to re-dock apps (the empty
            state's hint covers the all-undocked case above). */}
        {order.length > 0 && !order.includes('library') && (
          <button
            type="button"
            onClick={() => {
              navigate('/apps/library');
              onClose();
            }}
            className="mt-4 flex w-full items-center justify-center gap-2 rounded-xl border border-dashed border-border px-3 py-3 text-[12.5px] font-medium text-muted transition-colors hover:border-cyan/60 hover:text-foreground"
          >
            <LayoutGrid className="size-4 shrink-0 text-cyan" />
            <span>{t('apps.launcher.openLibrary')}</span>
          </button>
        )}

        {/* Footer chip row — the absorbed More-page content. 设置 navigates; the
            rest open a small overflow sheet. */}
        <div className="mt-4 flex items-stretch gap-2 border-t border-border pt-3">
          <Link to="/admin/dashboard" onClick={onClose} className={chipClass}>
            <Settings className="size-4 shrink-0" />
            <span className="truncate">{t('more.controlPanel')}</span>
          </Link>
          {email && (
            <button type="button" onClick={() => setSheet('account')} className={chipClass}>
              <UserRound className="size-4 shrink-0" />
              <span className="truncate">{t('more.account')}</span>
            </button>
          )}
          <button type="button" onClick={() => setSheet('appearance')} className={chipClass}>
            <Sun className="size-4 shrink-0" />
            <span className="truncate">{t('more.appearance')}</span>
          </button>
          <button type="button" onClick={() => setSheet('more')} className={chipClass}>
            <MoreHorizontal className="size-4 shrink-0" />
            <span className="truncate">{t('nav.more')}</span>
          </button>
        </div>
      </div>

      {/* Long-press manage sheet — a bottom action sheet with its OWN backdrop
          above the drawer (z-[60]), so a tap outside the actions dismisses it
          instead of falling through to the tiles/chips below. */}
      {menu && (
        <div className="fixed inset-0 z-[60]" role="dialog" aria-modal="true">
          <button
            type="button"
            aria-label={t('common.close')}
            onClick={() => setMenu(null)}
            className="absolute inset-0 bg-background/70 backdrop-blur-sm"
          />
          <div className="absolute inset-x-0 bottom-0 rounded-t-3xl border-t border-border bg-surface px-3 pb-[calc(1rem+env(safe-area-inset-bottom))] pt-2">
            <div className="mx-auto mb-2 h-1 w-10 rounded-full bg-border" />
            <div className="truncate px-2 pb-1 text-center text-[12px] font-medium text-muted">{menu.label}</div>
            <button
              type="button"
              onClick={() => {
                void undock(menu.item.id);
                setMenu(null);
              }}
              className="flex w-full items-center gap-3 rounded-xl px-3 py-3 text-left text-[14px] font-medium text-destructive transition hover:bg-destructive/[0.08]"
            >
              <PinOff className="size-[18px] shrink-0" />
              {t('apps.dock.unpin')}
            </button>
            {menu.item.kind === 'showpage' && (
              <button
                type="button"
                onClick={() => {
                  if (menu.item.kind === 'showpage') void unpin(menu.item.sessionId);
                  setMenu(null);
                }}
                className="flex w-full items-center gap-3 rounded-xl px-3 py-3 text-left text-[14px] font-medium text-destructive transition hover:bg-destructive/[0.08]"
              >
                <Minus className="size-[18px] shrink-0" />
                {t('library.apps.remove')}
              </button>
            )}
          </div>
        </div>
      )}

      {/* Footer overflow sheet (账号 / 外观 / 更多) — a focused bottom modal above
          the drawer. Backdrop closes only the sheet, returning to the drawer. */}
      {sheet && (
        <div className="fixed inset-0 z-[60]" role="dialog" aria-modal="true">
          <button
            type="button"
            aria-label={t('common.close')}
            onClick={() => setSheet(null)}
            className="absolute inset-0 bg-background/70 backdrop-blur-sm"
          />
          <div className="absolute inset-x-0 bottom-0 max-h-[82dvh] overflow-y-auto rounded-t-3xl border-t border-border bg-surface px-4 pb-[calc(1.25rem+env(safe-area-inset-bottom))] pt-2">
            <div className="mx-auto mb-2 h-1 w-10 rounded-full bg-border" />
            <div className="relative flex items-center justify-center py-1">
              <span className="text-[13px] font-semibold text-foreground">{sheetTitle[sheet]}</span>
              <button
                type="button"
                aria-label={t('common.close')}
                onClick={() => setSheet(null)}
                className="absolute right-0 top-0 grid size-8 place-items-center rounded-lg text-muted transition-colors hover:bg-foreground/[0.06] hover:text-foreground"
              >
                <X className="size-4" />
              </button>
            </div>
            <div className="pt-2">
              {sheet === 'account' && <MoreAccountSection />}
              {sheet === 'appearance' && <MoreAppearanceSection />}
              {sheet === 'more' && <MoreConnectionSection />}
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
