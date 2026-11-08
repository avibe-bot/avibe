import type { ReactNode } from 'react';

import type { WorkbenchFixtures } from './shell/fixtures';

export type DesignTheme = 'light' | 'dark';
export type DesignLang = 'zh' | 'en';
/** proposed: still being designed; approved: the owner signed it off; shipped: it renders the shipped components only. */
export type FrameStatus = 'proposed' | 'approved' | 'shipped';
/** Where a frame's router starts. */
export type FrameRoute = string | { pathname: string; search?: string; state?: unknown };
/** A phone's safe-area insets in CSS px: the status bar or Dynamic Island above, the home indicator below. */
export type FrameSafeArea = { top: number; bottom: number };

export interface FrameSpec {
  /** Unique within its file; the frame id is `<file path under frames/>/<name>`. */
  name: string;
  title: string;
  width: number;
  height: number;
  status: FrameStatus;
  /** Defaults to both themes. */
  themes?: DesignTheme[];
  /** Data the real providers serve to the frame. */
  fixtures?: WorkbenchFixtures;
  /** Initial route for components that read the location: a path (with any query), or a location that
   *  also carries router state a page reads on arrival (`{ pathname, state }`). */
  route?: FrameRoute;
  /** A CSS selector the screenshot hovers before capturing, for hover states (cards, menus). */
  hover?: string;
  /** What the screenshot clicks, in order, before capturing: a state only a click opens (a menu, a
   *  confirmation, an expanded section). Playwright selectors (CSS, `role=…`, `text=…`); `{t:<key>}`
   *  stands for that copy in the frame's language, so a selector by name works in zh and en:
   *  `role=button[name="{t:settings.models.upstream.addSubscription}"]`. Prefer a fixture or the
   *  page's URL state when the product offers one. */
  click?: string[];
  /** Milliseconds the frame waits, once its reads have settled, before it is ready: for a read that starts
   *  on a timer (a debounced search), so the screenshot shows its result rather than its spinner. The
   *  screenshot waits it out again after each `click`, for a click that starts a timed transition (setup's
   *  screen handoff). */
  wait?: number;
  /** A CSS selector the screenshot holds a pointer down on before capturing, for a pressed state
   *  (`:active`): after any `click` and `hover`, until the shot is taken. */
  press?: string;
  /** The screenshot asks for reduced motion (`prefers-reduced-motion: reduce`), for a moment's reduced
   *  form; the canvas follows the system setting. */
  reducedMotion?: boolean;
  /** For a frame that plays the product's own one-shot motion (keyframes named `motion-*` in
   *  `src/index.css`): the canvas starts it again every `replay` ms, so it loops. The screenshot shows
   *  where it settles. */
  replay?: number;
  /** The device's safe-area insets, for a phone that draws the page edge to edge (the phone app, a
   *  Home Screen web app): the screenshot has the browser report them, so `env(safe-area-inset-*)`
   *  reads them as that phone does. The canvas, a plain browser, reads 0, so an exported page shows
   *  such a frame as its screenshot. */
  safeArea?: FrameSafeArea;
  render: () => ReactNode;
}

export interface Frame extends FrameSpec {
  id: string;
  file: string;
}

/** A design file default-exports the frames it draws, usually one per state. */
export const frames = (specs: FrameSpec[]): FrameSpec[] => specs;
