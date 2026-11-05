/** @vitest-environment jsdom */

import { cleanup, render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { BotsContext, type BotsState } from '../../context/BotsContext';
import type { Bot } from '../../lib/bots';

const prefetch = vi.hoisted(() => vi.fn());
vi.mock('../../lib/chatPrefetch', () => ({
  CHAT_PREFETCH_CANDIDATES: 5,
  prefetchChatSnapshots: prefetch,
}));

import { useChatPrefetch } from './useChatPrefetch';

// The hook offers what the Inbox surface already shows, and only once that
// surface is active and its feed read has answered, with the main Bot's
// conversation pinned and the chat on screen named.

const Probe = ({ rows, ready }: { rows: Array<{ session_id: string }>; ready: boolean }) => {
  useChatPrefetch(rows, ready);
  return null;
};

const mainBot = { handle: 'main', session_id: 'ses_main', builtin: true } as Bot;
const withBots = (state: Partial<BotsState> | null, path: string, child: React.ReactNode) => {
  const routed = <MemoryRouter initialEntries={[path]}>{child}</MemoryRouter>;
  return state ? <BotsContext.Provider value={state as BotsState}>{routed}</BotsContext.Provider> : routed;
};

const rows = (count: number) => Array.from({ length: count }, (_, i) => ({ session_id: `ses_${i}` }));
const recent = ['ses_0', 'ses_1', 'ses_2', 'ses_3', 'ses_4'];

afterEach(() => {
  cleanup();
  prefetch.mockReset();
});

describe('useChatPrefetch', () => {
  it('offers the five most recent Sessions once the surface is ready', () => {
    const feed = rows(8);
    const view = render(withBots(null, '/inbox', <Probe rows={feed} ready={false} />));
    expect(prefetch).not.toHaveBeenCalled();

    view.rerender(withBots(null, '/inbox', <Probe rows={feed} ready />));
    expect(prefetch).toHaveBeenCalledTimes(1);
    expect(prefetch).toHaveBeenCalledWith(recent, { pinned: null, open: null });

    // A new feed with the same leading rows offers nothing new.
    view.rerender(withBots(null, '/inbox', <Probe rows={[...feed.slice(0, 5), { session_id: 'ses_new_tail' }]} ready />));
    expect(prefetch).toHaveBeenCalledTimes(1);
  });

  it.each([
    { path: '/inbox', open: null },
    { path: '/chat/ses_main', open: 'ses_main' },
    { path: '/chat/ses_3', open: 'ses_3' },
  ])('pins the main Bot conversation on $path and names the chat on screen', ({ path, open }) => {
    render(withBots({ mainBot }, path, <Probe rows={rows(8)} ready />));
    expect(prefetch).toHaveBeenCalledWith(recent, { pinned: 'ses_main', open });
  });

  it('offers the main Bot conversation even when the feed is empty', () => {
    render(withBots({ mainBot }, '/inbox', <Probe rows={[]} ready />));
    expect(prefetch).toHaveBeenCalledWith([], { pinned: 'ses_main', open: null });
  });
});
