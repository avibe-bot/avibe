import { renderToStaticMarkup } from 'react-dom/server';
import { Archive, EyeOff, GitFork, Pencil, Pin } from 'lucide-react';
import { describe, expect, it } from 'vitest';

import { Popover, PopoverTrigger } from '../ui/popover';
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

const countButtons = (markup: string) => (markup.match(/<button/g) ?? []).length;

describe('SessionActionMenu', () => {
  // ── Codex review (sessionActions.tsx:101) ───────────────────────────────────
  // The body used to claim role="menu" / role="menuitem" inside a Radix Popover,
  // which is a dialog: that announces a menu keyboard contract (one tab stop,
  // type-ahead) the popover does not implement. It is a labelled group of buttons
  // now — what it actually is.
  it('exposes a labelled group of buttons rather than a fake menu', () => {
    const html = renderMenu(fullMenu());

    expect(html).toContain('role="group"');
    expect(html).toContain('aria-label="Session actions"');
    expect(html).not.toContain('role="menu"');
    expect(html).not.toContain('role="menuitem"');
    expect(countButtons(html)).toBe(5);
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

  // ── Codex review (sessionActions.tsx:93) ────────────────────────────────────
  // A natively ``disabled`` button cannot be focused, so the only place the reason
  // lived — the ``title`` tooltip — was unreachable by keyboard AND by touch. The
  // row stays focusable via aria-disabled and states the reason on screen.
  it('keeps an unforkable session’s fork row focusable and explains it on screen', () => {
    const reason = 'This session has no native agent session to fork yet.';
    const html = renderMenu([
      row({ id: 'fork', group: 'continue', label: 'Fork session', icon: GitFork, disabled: true, title: reason }),
    ]);

    expect(html).toContain('aria-disabled="true"');
    expect(html).not.toContain('disabled=""'); // focusable, so the reason is reachable
    expect(html).toContain(`title="${reason}"`);
    // Visible text, not only the tooltip: touch pointers never get a tooltip.
    expect(html).toContain(`>${reason}</span>`);
    expect(html).toContain('Fork session');
  });

  it('swaps a pending row’s icon for a spinner', () => {
    const html = renderMenu([row({ id: 'pin', group: 'organize', label: 'Pin to top', pending: true, disabled: true })]);

    expect(html).toContain('animate-spin');
  });

  it('renders nothing for an empty action list (read-only session)', () => {
    const html = renderMenu([]);

    expect(countButtons(html)).toBe(0);
  });
});

describe('SessionActionsTrigger', () => {
  // ── Codex review (sessionActions.tsx:67) ────────────────────────────────────
  // The trigger used to hand-write aria-haspopup="menu"; <PopoverTrigger asChild>
  // spreads its own props LAST, so the shipped button always said "dialog" and only
  // a direct render (the old test) ever saw "menu". The composed render is the truth.
  it('takes its popup semantics from the popover it triggers', () => {
    const composed = renderToStaticMarkup(
      <Popover open>
        <PopoverTrigger asChild>
          <SessionActionsTrigger label="Session actions" open />
        </PopoverTrigger>
      </Popover>,
    );

    expect(composed).toContain('aria-label="Session actions"');
    expect(composed).toContain('aria-haspopup="dialog"');
    expect(composed).toContain('aria-expanded="true"');
    expect(composed).toContain('data-state="open"');

    const closed = renderToStaticMarkup(
      <Popover open={false}>
        <PopoverTrigger asChild>
          <SessionActionsTrigger label="Session actions" open={false} />
        </PopoverTrigger>
      </Popover>,
    );
    expect(closed).toContain('aria-expanded="false"');

    // Rendered on its own it claims no popup at all — no second, contradictory
    // source of truth for what the button opens.
    const bare = renderToStaticMarkup(<SessionActionsTrigger label="Session actions" open={false} />);
    expect(bare).toContain('aria-label="Session actions"');
    expect(bare).not.toContain('aria-haspopup');
    expect(bare).not.toContain('aria-expanded');
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
