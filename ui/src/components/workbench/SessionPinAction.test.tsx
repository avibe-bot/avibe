import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { SessionPinAction, SessionPinIndicator } from './SessionPinAction';
import { sessionPinRowPaddingClass } from './sessionPinLayout';

const renderAction = (pinned: boolean, pending = false) =>
  renderToStaticMarkup(
    <SessionPinAction
      pinned={pinned}
      pending={pending}
      pinLabel="Pin to top"
      unpinLabel="Unpin"
      onToggle={() => undefined}
    />,
  );

describe('SessionPinAction', () => {
  it('keeps the 24px action target out of the session row layout', () => {
    const html = renderAction(false);

    expect(html).toContain('absolute inset-y-0 right-2 flex items-center');
    expect(html).toContain('grid size-6');
  });

  it('reveals an unpinned action on row hover, keyboard focus, and coarse pointers', () => {
    const html = renderAction(false);

    expect(html).toContain('aria-pressed="false"');
    expect(html).toContain('opacity-0');
    expect(html).toContain('group-hover/sess:opacity-100');
    expect(html).toContain('group-focus-within/sess:opacity-100');
    expect(html).toContain('pointer-coarse:opacity-100');
  });

  it('keeps a pinned action visible without a resting background and gives hover feedback', () => {
    const html = renderAction(true);

    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('opacity-100');
    expect(html).not.toMatch(/(?:class="|\s)bg-[^\s"]+/);
    expect(html).toContain('hover:bg-cyan/[0.18]');
    expect(html).toContain('hover:scale-105');
    expect(html).toContain('group-hover/pin:-rotate-12');
  });

  it('shows a disabled progress state while persistence is pending', () => {
    const html = renderAction(false, true);

    expect(html).toContain('disabled=""');
    expect(html).toContain('animate-spin');
    expect(html).toContain('cursor-wait');
  });
});

describe('SessionPinIndicator', () => {
  it('renders no passive status for an unpinned session', () => {
    const html = renderToStaticMarkup(<SessionPinIndicator pinned={false} label="Pinned" />);

    expect(html).toBe('');
  });

  it('renders a non-interactive status icon for a pinned session', () => {
    const html = renderToStaticMarkup(<SessionPinIndicator pinned label="Pinned" />);

    expect(html).toContain('role="img"');
    expect(html).toContain('aria-label="Pinned"');
    expect(html).toContain('text-cyan');
    expect(html).not.toMatch(/(?:class="|\s)bg-[^\s"]+/);
    expect(html).not.toContain('<button');
  });
});

describe('sessionPinRowPaddingClass', () => {
  it('uses the full title width until an unpinned action is revealed', () => {
    const className = sessionPinRowPaddingClass(false);

    expect(className).toContain('pr-2.5');
    expect(className).toContain('hover:pr-10');
    expect(className).toContain('focus-within:pr-10');
    expect(className).toContain('pointer-coarse:pr-10');
  });

  it('always reserves action space for a pinned session', () => {
    expect(sessionPinRowPaddingClass(true)).toBe('pr-10');
  });
});
