/* @vitest-environment jsdom */

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';
import { Popover, PopoverContent } from './popover';

afterEach(cleanup);

describe('PopoverContent', () => {
  it('withdraws inactive content without changing the owner open state', () => {
    const onOpenChange = vi.fn();
    const view = render(
      <RouteSurfaceActiveContext.Provider value>
        <Popover open onOpenChange={onOpenChange}>
          <PopoverContent>Menu content</PopoverContent>
        </Popover>
      </RouteSurfaceActiveContext.Provider>,
    );
    expect(screen.getByText('Menu content')).toBeTruthy();

    view.rerender(
      <RouteSurfaceActiveContext.Provider value={false}>
        <Popover open onOpenChange={onOpenChange}>
          <PopoverContent>Menu content</PopoverContent>
        </Popover>
      </RouteSurfaceActiveContext.Provider>,
    );

    expect(screen.queryByText('Menu content')).toBeNull();
    expect(onOpenChange).not.toHaveBeenCalled();

    view.rerender(
      <RouteSurfaceActiveContext.Provider value>
        <Popover open onOpenChange={onOpenChange}>
          <PopoverContent>Menu content</PopoverContent>
        </Popover>
      </RouteSurfaceActiveContext.Provider>,
    );
    expect(screen.getByText('Menu content')).toBeTruthy();
  });
});
