/* @vitest-environment jsdom */

import { act, cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

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
});
