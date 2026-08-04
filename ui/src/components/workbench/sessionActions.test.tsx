import { renderToStaticMarkup } from 'react-dom/server';
import { Archive, EyeOff, GitFork, Pencil, Pin } from 'lucide-react';
import { describe, expect, it } from 'vitest';

import { SessionActionMenu, SessionActionsTrigger, type SessionActionDescriptor } from './sessionActions';

const row = (over: Partial<SessionActionDescriptor> & Pick<SessionActionDescriptor, 'id' | 'group' | 'label'>) =>
  ({ icon: Pin, onSelect: () => undefined, ...over }) as SessionActionDescriptor;

// The shape ``useSessionActions`` produces for a live, forkable session.
const fullMenu = (): SessionActionDescriptor[] => [
  row({ id: 'pin', group: 'organize', label: 'Pin to top', icon: Pin }),
  row({ id: 'rename', group: 'organize', label: 'Rename', icon: Pencil }),
  row({ id: 'fork', group: 'continue', label: 'Fork session', icon: GitFork }),
  row({ id: 'hide', group: 'continue', label: 'Hide to background', icon: EyeOff }),
  row({ id: 'archive', group: 'lifecycle', label: 'Archive session', icon: Archive, danger: true, hint: '⇧⌘D' }),
];

const renderMenu = (actions: SessionActionDescriptor[]) =>
  renderToStaticMarkup(<SessionActionMenu actions={actions} label="Session actions" />);

describe('SessionActionMenu', () => {
  it('exposes menu semantics for every row', () => {
    const html = renderMenu(fullMenu());

    expect(html).toContain('role="menu"');
    expect(html).toContain('aria-label="Session actions"');
    expect(html.match(/role="menuitem"/g)).toHaveLength(5);
    expect(html).toContain('Pin to top');
    expect(html).toContain('Rename');
    expect(html).toContain('Fork session');
    expect(html).toContain('Hide to background');
    expect(html).toContain('Archive session');
  });

  it('draws one hairline divider per group change, never above the first row', () => {
    const html = renderMenu(fullMenu());

    // organize → continue and continue → lifecycle; pin/rename and fork/hide pair up.
    expect(html.match(/border-t border-border/g)).toHaveLength(2);
    expect(html.indexOf('border-t border-border')).toBeGreaterThan(html.indexOf('Pin to top'));
  });

  it('styles archive as destructive and shows its keyboard hint', () => {
    const html = renderMenu(fullMenu());

    expect(html).toContain('text-pink');
    expect(html).toContain('⇧⌘D');
    expect(html).toContain('font-mono');
  });

  it('keeps an unforkable session’s fork row visible, disabled, and explained', () => {
    const html = renderMenu([
      row({
        id: 'fork',
        group: 'continue',
        label: 'Fork session',
        icon: GitFork,
        disabled: true,
        title: 'This session has no native agent session to fork yet.',
      }),
    ]);

    expect(html).toContain('disabled=""');
    expect(html).toContain('title="This session has no native agent session to fork yet."');
    expect(html).toContain('Fork session');
  });

  it('swaps a pending row’s icon for a spinner', () => {
    const html = renderMenu([row({ id: 'pin', group: 'organize', label: 'Pin to top', pending: true, disabled: true })]);

    expect(html).toContain('animate-spin');
  });

  it('renders nothing for an empty action list (read-only session)', () => {
    const html = renderMenu([]);

    expect(html).not.toContain('role="menuitem"');
  });
});

describe('SessionActionsTrigger', () => {
  it('announces itself as a menu button and mirrors the open state', () => {
    const closed = renderToStaticMarkup(<SessionActionsTrigger label="Session actions" open={false} />);
    const open = renderToStaticMarkup(<SessionActionsTrigger label="Session actions" open />);

    expect(closed).toContain('aria-haspopup="menu"');
    expect(closed).toContain('aria-label="Session actions"');
    expect(closed).toContain('aria-expanded="false"');
    expect(open).toContain('aria-expanded="true"');
  });

  it('hides the row variant until hover, keyboard focus, a coarse pointer, or an open menu', () => {
    const closed = renderToStaticMarkup(<SessionActionsTrigger label="Session actions" open={false} />);

    expect(closed).toContain('opacity-0');
    expect(closed).toContain('group-hover/sess:opacity-100');
    expect(closed).toContain('group-focus-within/sess:opacity-100');
    expect(closed).toContain('pointer-coarse:opacity-100');
    expect(renderToStaticMarkup(<SessionActionsTrigger label="Session actions" open />)).toContain('opacity-100');
  });

  it('keeps the bar variant (chat header, mobile row) permanently visible', () => {
    const html = renderToStaticMarkup(<SessionActionsTrigger label="Session actions" open={false} variant="bar" />);

    expect(html).not.toContain('opacity-0');
    expect(html).toContain('size-7');
  });
});
