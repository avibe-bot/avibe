/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import type { TFunction } from 'i18next';

import type { InboxSession } from '../../context/ApiContext';
import { InboxCard } from './InboxCard';

// The Inbox card opens its conversation when it is pressed (the rule is `useCardOpen`'s): its title is
// the link, a press anywhere else on it follows that link, its own 「Mark read」 marks read and stays,
// a drag that selects text selects, and a modified or middle press opens a new tab.

vi.mock('../ui/markdown', () => ({
  // As the real renderer does with `interactive={false}`: a link's text, not an anchor.
  Markdown: ({ content }: { content: string }) => <p>{content.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')}</p>,
}));

const t = ((key: string) => key) as unknown as TFunction;
const session = (unread = 2): InboxSession => ({
  session_id: 'ses_a', title: 'Fix the cache', scope_id: null, project_id: 'proj', project_name: 'avibe',
  last_activity_at: '2026-11-06T05:00:00Z', last_message_author: 'agent', replied: false,
  preview_text: 'Opened [PR #3141](https://github.com/avibe-bot/avibe/pull/3141), waiting on review.',
  preview_at: null, unread_count: unread, unread: unread > 0,
});

const onOpen = vi.fn();
const onMarkRead = vi.fn();
const card = (unread = 2) => (
  <MemoryRouter>
    <InboxCard
      session={session(unread)}
      unread={unread}
      quiet={false}
      inBotSection={false}
      bot={undefined}
      botLabel=""
      canChat
      minute={0}
      t={t}
      onOpen={onOpen}
      onMarkRead={onMarkRead}
    />
  </MemoryRouter>
);
const article = () => document.querySelector<HTMLElement>('[data-inbox-session-id="ses_a"]')!;
const preview = () => screen.getByText(/waiting on review/);

let opened: unknown[][] = [];
beforeEach(() => {
  onOpen.mockClear();
  onMarkRead.mockClear();
  opened = [];
  vi.spyOn(window, 'open').mockImplementation((...args: unknown[]) => {
    opened.push(args);
    return null;
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('an Inbox card', () => {
  it('opens its conversation when it is clicked or tapped anywhere on it', () => {
    render(card());
    fireEvent.click(preview());
    fireEvent.click(article().querySelector('span')!); // the project chip
    fireEvent.click(article());
    expect(onOpen).toHaveBeenCalledTimes(3);
    expect(onOpen).toHaveBeenLastCalledWith(session());
  });

  it('is a link named for its conversation: focusable, and Space opens it as Enter does', () => {
    render(card());
    const link = within(article()).getByRole('link', { name: 'Fix the cache' });
    expect(link.getAttribute('href')).toBe('/chat/ses_a');
    fireEvent.keyDown(link, { key: ' ' });
    expect(onOpen).toHaveBeenCalledTimes(1);
    // A plain click on the link opens once, through the page's own navigation.
    fireEvent.click(link);
    expect(onOpen).toHaveBeenCalledTimes(2);
  });

  it('marks read from 「Mark read」 without opening', () => {
    render(card());
    fireEvent.click(screen.getByRole('button', { name: 'workbench.inbox.markRead' }));
    expect(onMarkRead).toHaveBeenCalledWith('ses_a');
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('shows a link in the preview as text, which opens the conversation like the rest of the card', () => {
    render(card());
    expect(article().querySelectorAll('a')).toHaveLength(1); // the title only
    fireEvent.click(screen.getByText(/PR #3141/));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it('does not open at the end of a drag: one that moved, or one that selected text', () => {
    render(card());
    fireEvent.pointerDown(preview(), { clientX: 10, clientY: 10 });
    fireEvent.click(preview(), { clientX: 60, clientY: 12 });
    expect(onOpen).not.toHaveBeenCalled();

    fireEvent.pointerDown(preview(), { clientX: 10, clientY: 10 });
    vi.spyOn(window, 'getSelection').mockReturnValue({
      isCollapsed: false,
      anchorNode: preview().firstChild,
      toString: () => 'waiting on review',
    } as unknown as Selection);
    fireEvent.click(preview(), { clientX: 10, clientY: 10 });
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('opens in a new tab on a Cmd- or Ctrl-click or a middle click, as its link would', () => {
    render(card());
    fireEvent.click(preview(), { metaKey: true });
    fireEvent.click(preview(), { ctrlKey: true });
    fireEvent(preview(), new MouseEvent('auxclick', { bubbles: true, cancelable: true, button: 1 }));
    expect(onOpen).not.toHaveBeenCalled();
    expect(opened).toEqual([
      ['/chat/ses_a', '_blank', 'noopener'],
      ['/chat/ses_a', '_blank', 'noopener'],
      ['/chat/ses_a', '_blank', 'noopener'],
    ]);
  });

  it('shows its unread count in the header beside the time, and drops 「Mark read」 once read', () => {
    const view = render(card(3));
    const header = article().firstElementChild as HTMLElement;
    expect(within(header).getByText('3').hasAttribute('data-inbox-unread-count')).toBe(true);
    view.rerender(card(0));
    expect(article().querySelector('[data-inbox-unread-count]')).toBeNull();
    expect(screen.queryByRole('button', { name: 'workbench.inbox.markRead' })).toBeNull();
  });
});
