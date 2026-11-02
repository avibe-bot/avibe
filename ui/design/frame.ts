import type { ReactNode } from 'react';

import type { WorkbenchFixtures } from './shell/fixtures';

export type DesignTheme = 'light' | 'dark';
export type DesignLang = 'zh' | 'en';
/** proposed: still being designed; approved: the owner signed it off; shipped: it renders the shipped components only. */
export type FrameStatus = 'proposed' | 'approved' | 'shipped';

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
  /** Initial route for components that read the location. */
  route?: string;
  /** A CSS selector the screenshot hovers before capturing, for hover states (cards, menus). */
  hover?: string;
  /** What the screenshot clicks, in order, before capturing: a state only a click opens (a menu, a
   *  confirmation, an expanded section). Playwright selectors (CSS, `role=…`, `text=…`); `{t:<key>}`
   *  stands for that copy in the frame's language, so a selector by name works in zh and en:
   *  `role=button[name="{t:settings.models.upstream.addSubscription}"]`. Prefer a fixture or the
   *  page's URL state when the product offers one. */
  click?: string[];
  /** Milliseconds the frame waits, once its reads have settled, before it is ready: for a read that starts
   *  on a timer (a debounced search), so the screenshot shows its result rather than its spinner. */
  wait?: number;
  render: () => ReactNode;
}

export interface Frame extends FrameSpec {
  id: string;
  file: string;
}

/** A design file default-exports the frames it draws, usually one per state. */
export const frames = (specs: FrameSpec[]): FrameSpec[] => specs;
