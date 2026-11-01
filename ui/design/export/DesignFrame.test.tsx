/* @vitest-environment jsdom */

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DesignFrame } from './DesignFrame';

type State = { enabled: boolean };

// The Show Page's public annotation API, as the overlay attaches it (contract §2).
function installAnnotationApi(initial: boolean) {
  let state: State = { enabled: initial };
  const listeners = new Set<(next: State) => void>();
  (globalThis as Record<string, unknown>).__AVIBE_SHOW__ = {
    annotation: {
      api: {
        getState: () => state,
        subscribe: (callback: (next: State) => void) => {
          listeners.add(callback);
          return () => listeners.delete(callback);
        },
      },
    },
  };
  return (enabled: boolean) => {
    state = { enabled };
    act(() => listeners.forEach((listener) => listener(state)));
  };
}

const index = {
  lang: 'zh',
  shots: [
    { id: 'sidebar/bots/three', title: '伙伴 · 3 个伙伴', status: 'approved', theme: 'light', width: 248, height: 190, hash: 'h1', src: 'shots/sidebar/bots/three.light.png' },
    { id: 'sidebar/bots/three', title: '伙伴 · 3 个伙伴', status: 'approved', theme: 'dark', lang: 'en', width: 248, height: 190, hash: 'h2', src: 'shots/sidebar/bots/three.dark.png' },
  ],
};

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(index))));
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete (globalThis as Record<string, unknown>).__AVIBE_SHOW__;
});

describe('DesignFrame', () => {
  it('runs each theme live, and shows screenshots the overlay can reach while annotation is on', async () => {
    const setAnnotating = installAnnotationApi(false);
    render(<DesignFrame id="sidebar/bots/three" />);

    const frames = await screen.findAllByTitle(/伙伴 · 3 个伙伴/);
    expect(frames.map((frame) => frame.getAttribute('src'))).toEqual([
      './design/canvas/design.html?frame=sidebar%2Fbots%2Fthree&theme=light&lang=zh',
      // A shot exported in another language keeps it (partial exports merge).
      './design/canvas/design.html?frame=sidebar%2Fbots%2Fthree&theme=dark&lang=en',
    ]);

    setAnnotating(true);
    const shots = screen.getAllByRole('img');
    expect(shots.map((shot) => [shot.getAttribute('src'), shot.getAttribute('data-frame-id')])).toEqual([
      ['./design/shots/sidebar/bots/three.light.png?v=h1', 'sidebar/bots/three'],
      ['./design/shots/sidebar/bots/three.dark.png?v=h2', 'sidebar/bots/three'],
    ]);
    expect(document.querySelector('iframe')).toBeNull();

    setAnnotating(false);
    expect(screen.getAllByTitle(/伙伴 · 3 个伙伴/)).toHaveLength(2);
    expect(screen.queryAllByRole('img')).toHaveLength(0);
  });
});
