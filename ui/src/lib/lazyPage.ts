import { createElement, use } from 'react';
import type { ComponentType } from 'react';

/** A component whose code is its own chunk, fetched by `preload` or on first render. */
export type LazyPage<P extends object> = ComponentType<P> & { preload: () => Promise<ComponentType<P>> };

/**
 * A component loaded on demand (docs/plans/web-ui-first-load.md). `preload`
 * starts the fetch early — for the page the app opens on, and for the other
 * pages once the first screen is up — and once the code is here the component
 * renders at once, as itself (no wrapper, so nothing remounts when it arrives).
 * Before that it suspends to the nearest `<Suspense>`; a router navigation is a
 * transition, which keeps the current screen up meanwhile. A failed fetch goes
 * to the nearest error boundary, whose fallback offers the reload that can
 * recover it: a browser keeps a failed module fetch until the page reloads.
 */
export function lazyPage<P extends object>(load: () => Promise<ComponentType<P>>): LazyPage<P> {
  let loaded: ComponentType<P> | null = null;
  let pending: Promise<ComponentType<P>> | null = null;
  const preload = () => {
    pending ??= load().then((component) => {
      loaded = component;
      return component;
    });
    return pending;
  };
  const Page = (props: P) => createElement(loaded ?? use(preload()), props);
  return Object.assign(Page, { preload });
}
