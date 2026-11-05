/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import { Popover } from '../ui/popover';
import type { InboxFeedStatus } from '../../context/WorkbenchInboxContext';
import { InboxHoverPopover } from './InboxHoverPopover';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

// The sidebar's Inbox peek before it has rows (docs/plans/web-ui-motion.md §6):
// skeleton rows while the feed's first answer is owed, the failure with a retry
// when it failed, and "no conversations" only once the feed said so.

const mount = (feedStatus: InboxFeedStatus, onRetry = vi.fn()) => {
  render(
    <MemoryRouter>
      <Popover open>
        <InboxHoverPopover
          sessions={[]}
          unreadBySession={{}}
          unreadSessions={0}
          totalUnread={0}
          canMarkRead
          feedStatus={feedStatus}
          onRetry={onRetry}
          onItemClick={() => {}}
          onMarkAllRead={() => {}}
          onMouseEnter={() => {}}
          onMouseLeave={() => {}}
        />
      </Popover>
    </MemoryRouter>,
  );
  return onRetry;
};

afterEach(cleanup);

describe('the Inbox peek before it has rows', () => {
  it('draws skeleton rows while the first answer is owed, not "no conversations"', () => {
    mount('pending');
    expect(screen.getByRole('status').getAttribute('aria-busy')).toBe('true');
    expect(screen.queryByText('workbench.inbox.empty')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('says a failed read failed and offers a retry, not a skeleton that never ends', () => {
    const onRetry = mount('failed');
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByRole('alert').textContent).toContain('workbench.inbox.loadError');
    fireEvent.click(screen.getByRole('button', { name: 'common.retry' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('says it is empty once the feed answered with nothing', () => {
    mount('loaded');
    expect(screen.getByText('workbench.inbox.empty')).toBeTruthy();
    expect(screen.queryByRole('status')).toBeNull();
  });
});
