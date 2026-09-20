/* @vitest-environment jsdom */

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';
import { ContextMenu } from './context-menu';

afterEach(cleanup);

describe('ContextMenu', () => {
  it('closes and marks Escape as consumed', () => {
    const onClose = vi.fn();
    render(
      <ContextMenu x={0} y={0} onClose={onClose}>
        <button type="button">Menu item</button>
      </ContextMenu>,
    );
    const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });

    act(() => window.dispatchEvent(event));

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(event.defaultPrevented).toBe(true);
  });

  it('withdraws while inactive without closing the owner state', () => {
    const onClose = vi.fn();
    const view = render(
      <RouteSurfaceActiveContext.Provider value>
        <ContextMenu x={0} y={0} onClose={onClose}>
          <button type="button">Menu item</button>
        </ContextMenu>
      </RouteSurfaceActiveContext.Provider>,
    );

    expect(screen.getByRole('menu')).toBeTruthy();
    view.rerender(
      <RouteSurfaceActiveContext.Provider value={false}>
        <ContextMenu x={0} y={0} onClose={onClose}>
          <button type="button">Menu item</button>
        </ContextMenu>
      </RouteSurfaceActiveContext.Provider>,
    );

    const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
    act(() => window.dispatchEvent(event));

    expect(screen.queryByRole('menu')).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);

    view.rerender(
      <RouteSurfaceActiveContext.Provider value>
        <ContextMenu x={0} y={0} onClose={onClose}>
          <button type="button">Menu item</button>
        </ContextMenu>
      </RouteSurfaceActiveContext.Provider>,
    );
    expect(screen.getByRole('menu')).toBeTruthy();
  });
});
