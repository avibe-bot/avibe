/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import { Popover } from '../ui/popover';
import type { InboxSession } from '../../context/ApiContext';
import { DeferredInboxPreview, InboxHoverPopover } from './InboxHoverPopover';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../ui/markdown', () => ({ Markdown: ({ content }: { content: string }) => <p>{content}</p> }));

// The sidebar's Inbox peek opens a conversation the way an Inbox card does (`useCardOpen`): a press on
// the row, its title link (Space too), not the end of a drag, and a new tab on a modified press.

const row = (id: number): InboxSession => ({
  session_id: `session-${id}`, title: `Session ${id}`, scope_id: null, project_id: 'proj', project_name: 'avibe',
  last_activity_at: '2026-11-06T05:00:00Z', last_message_author: 'agent', replied: false,
  preview_text: `Preview ${id}`, preview_at: null, unread_count: 1, unread: true,
});
const onItemClick = vi.fn();
const mount = () => render(
  <MemoryRouter>
    <Popover open>
      <InboxHoverPopover
        sessions={[row(1), row(2)]}
        unreadBySession={{ 'session-1': 1 }}
        unreadSessions={1}
        totalUnread={1}
        canMarkRead
        onItemClick={onItemClick}
        onMarkAllRead={() => {}}
        onMouseEnter={() => {}}
        onMouseLeave={() => {}}
      />
    </Popover>
  </MemoryRouter>,
);
const peekRow = (id: number) => document.querySelector<HTMLElement>(`[data-inbox-peek-id="session-${id}"]`)!;

// The first-screen prefetch (routePages.ts) loads the preview before a hover opens the peek.
beforeAll(async () => {
  await DeferredInboxPreview.preload();
});

beforeEach(() => {
  onItemClick.mockClear();
  vi.spyOn(window, 'open').mockImplementation(() => null);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('a row of the Inbox peek', () => {
  it('opens its conversation when pressed anywhere on it', () => {
    mount();
    fireEvent.click(screen.getByText('Preview 2'));
    expect(onItemClick).toHaveBeenCalledWith(row(2));
    expect(peekRow(2).tagName).not.toBe('BUTTON');
  });

  it('is a link named for its conversation, and Space opens it', () => {
    mount();
    const link = screen.getByRole('link', { name: 'Session 1' });
    expect(link.getAttribute('href')).toBe('/chat/session-1');
    fireEvent.keyDown(link, { key: ' ' });
    expect(onItemClick).toHaveBeenCalledWith(row(1));
  });

  it('does not open at the end of a drag that selected text, and opens a new tab on a Cmd-click', () => {
    mount();
    vi.spyOn(window, 'getSelection').mockReturnValue({
      isCollapsed: false,
      anchorNode: screen.getByText('Preview 1').firstChild,
      toString: () => 'Preview',
    } as unknown as Selection);
    fireEvent.click(screen.getByText('Preview 1'));
    expect(onItemClick).not.toHaveBeenCalled();
    vi.mocked(window.getSelection).mockRestore();
    fireEvent.click(screen.getByText('Preview 1'), { metaKey: true });
    expect(onItemClick).not.toHaveBeenCalled();
    expect(window.open).toHaveBeenCalledWith('/chat/session-1', '_blank', 'noopener');
  });
});
