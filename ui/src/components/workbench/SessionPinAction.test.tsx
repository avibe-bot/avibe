import type { ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { SessionPinAction } from './SessionPinAction';
import {
  SESSION_ROW_ACTION_BUTTON_CLASS,
  SESSION_ROW_MENU_POSITION_CLASS,
  SESSION_ROW_PIN_POSITION_CLASS,
} from './sessionRowLayout';

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
  it('stops the row click and invokes the shared pin action', () => {
    const onToggle = vi.fn();
    const event = { stopPropagation: vi.fn() };
    const wrapper = SessionPinAction({
      pinned: false,
      pending: false,
      pinLabel: 'Pin to top',
      unpinLabel: 'Unpin',
      onToggle,
    }) as ReactElement<{ children: ReactElement<{ onClick: (event: typeof event) => void }> }>;
    const button = wrapper.props.children;

    button.props.onClick(event);

    expect(event.stopPropagation).toHaveBeenCalledTimes(1);
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it('uses the compact rounded rail geometry shared with the session action menu', () => {
    const html = renderAction(false);

    expect(html).toContain('absolute inset-y-0 flex items-center right-5');
    expect(html).toContain('grid shrink-0 place-items-center');
    expect(html).toContain('size-5 rounded-md');
    expect(SESSION_ROW_ACTION_BUTTON_CLASS).toBe('size-5 rounded-md');
    expect(SESSION_ROW_PIN_POSITION_CLASS).toBe('right-5');
    expect(SESSION_ROW_MENU_POSITION_CLASS).toBe('right-0');
  });

  it('reveals an unpinned action on row hover, keyboard focus, and coarse pointers', () => {
    const html = renderAction(false);

    expect(html).toContain('aria-pressed="false"');
    expect(html).toContain('opacity-0');
    expect(html).toContain('group-hover/sess:opacity-100');
    expect(html).toContain('group-focus-within/sess:opacity-100');
    expect(html).toContain('pointer-coarse:opacity-100');
  });

  it('keeps a pinned action visible with unpin semantics', () => {
    const html = renderAction(true);

    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('aria-label="Unpin"');
    expect(html).toContain('opacity-100');
    expect(html).toContain('hover:bg-cyan/[0.18]');
  });

  it('shows a disabled progress state while persistence is pending', () => {
    const html = renderAction(false, true);

    expect(html).toContain('disabled=""');
    expect(html).toContain('animate-spin');
    expect(html).toContain('cursor-wait');
  });
});
