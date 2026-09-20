import { DESKTOP_MIN_WIDTH } from './useIsDesktop';

/** The one width the sidebar, every desktop content offset and the Settings
 *  overlay edge read. Declared in `index.css` at MIN_SIDEBAR_WIDTH so the
 *  default shell needs no JS; `SidebarResizer` overrides it inline while it is
 *  mounted, which is also how the value reaches the overlay's portal. */
export const SIDEBAR_WIDTH_VAR = '--app-sidebar-w';
/** The shipped width is the minimum, so the default shell is unchanged. */
export const MIN_SIDEBAR_WIDTH = 248;
export const MAX_SIDEBAR_WIDTH = 496;

/**
 * What the shell keeps for whatever sits beside the sidebar — the workbench
 * content, or inline Settings, which is a 196px rail plus a pane inside this
 * same remainder. It is the narrowest desktop the shell supports minus the
 * sidebar's own shipped width, i.e. exactly what a default sidebar already
 * leaves at the `md` breakpoint, so no configuration the shell ships today
 * changes.
 *
 * `MAX_SIDEBAR_WIDTH` alone is viewport-blind: dragging to 496 in a 768px
 * window leaves 272px, which is a cramped workbench and an unusable inline
 * Settings (196 of that 272 is the rail). The maximum is the only place to say
 * this — capping the consumers instead would have each of them re-derive the
 * same budget, and capping the sidebar only while Settings is open would move
 * the column the moment Settings opened.
 */
export const MIN_WIDTH_BESIDE_SIDEBAR = DESKTOP_MIN_WIDTH - MIN_SIDEBAR_WIDTH;

/** Never below the minimum: under `md` no sidebar is rendered at all, so a
 *  viewport that cannot afford even the default has none to constrain. */
export const maxSidebarWidth = (viewportWidth: number) => Math.max(
  MIN_SIDEBAR_WIDTH,
  Math.min(MAX_SIDEBAR_WIDTH, viewportWidth - MIN_WIDTH_BESIDE_SIDEBAR),
);

export const currentViewportWidth = () => (
  typeof window === 'undefined' ? Number.POSITIVE_INFINITY : window.innerWidth
);

export const clampSidebarWidth = (
  width: number,
  viewportWidth = currentViewportWidth(),
) => Math.min(
  maxSidebarWidth(viewportWidth),
  Math.max(MIN_SIDEBAR_WIDTH, Math.round(width)),
);
