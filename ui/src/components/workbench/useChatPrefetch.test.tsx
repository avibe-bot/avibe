/** @vitest-environment jsdom */

import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const prefetch = vi.hoisted(() => vi.fn());
vi.mock('../../lib/chatPrefetch', () => ({
  CHAT_PREFETCH_CANDIDATES: 5,
  prefetchChatSnapshots: prefetch,
}));

import { useChatPrefetch } from './useChatPrefetch';

// The hook offers what the Inbox surface already shows, and only once that
// surface is active and its feed read has answered.

const Probe = ({ rows, ready }: { rows: Array<{ session_id: string }>; ready: boolean }) => {
  useChatPrefetch(rows, ready);
  return null;
};

const rows = (count: number) => Array.from({ length: count }, (_, i) => ({ session_id: `ses_${i}` }));

afterEach(() => {
  cleanup();
  prefetch.mockReset();
});

describe('useChatPrefetch', () => {
  it('offers the five most recent Sessions once the surface is ready', () => {
    const feed = rows(8);
    const view = render(<Probe rows={feed} ready={false} />);
    expect(prefetch).not.toHaveBeenCalled();

    view.rerender(<Probe rows={feed} ready />);
    expect(prefetch).toHaveBeenCalledTimes(1);
    expect(prefetch).toHaveBeenCalledWith(['ses_0', 'ses_1', 'ses_2', 'ses_3', 'ses_4']);

    // A new feed with the same leading rows offers nothing new.
    view.rerender(<Probe rows={[...feed.slice(0, 5), { session_id: 'ses_new_tail' }]} ready />);
    expect(prefetch).toHaveBeenCalledTimes(1);
  });
});
