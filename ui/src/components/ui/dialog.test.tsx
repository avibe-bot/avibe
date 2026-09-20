/* @vitest-environment jsdom */

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';
import { Dialog, DialogContent, DialogTitle } from './dialog';

afterEach(cleanup);

describe('DialogContent mobile sheet', () => {
  it('provides the shared tall height and bottom-up motion', () => {
    render(
      <Dialog open>
        <DialogContent aria-describedby={undefined} mobileSheetHeight="tall">
          <DialogTitle>Preview</DialogTitle>
        </DialogContent>
      </Dialog>,
    );

    const dialog = screen.getByRole('dialog');
    expect(dialog.className).toContain('max-md:h-[90dvh]');
    expect(dialog.className).toContain('max-md:data-[state=open]:slide-in-from-bottom');
    expect(dialog.className).toContain('max-md:data-[state=closed]:slide-out-to-bottom');
  });

  it('withdraws inactive content without changing the owner open state', () => {
    const onOpenChange = vi.fn();
    const view = render(
      <RouteSurfaceActiveContext.Provider value>
        <Dialog open onOpenChange={onOpenChange}>
          <DialogContent aria-describedby={undefined}>
            <DialogTitle>Preview</DialogTitle>
          </DialogContent>
        </Dialog>
      </RouteSurfaceActiveContext.Provider>,
    );
    expect(screen.getByRole('dialog')).toBeTruthy();

    view.rerender(
      <RouteSurfaceActiveContext.Provider value={false}>
        <Dialog open onOpenChange={onOpenChange}>
          <DialogContent aria-describedby={undefined}>
            <DialogTitle>Preview</DialogTitle>
          </DialogContent>
        </Dialog>
      </RouteSurfaceActiveContext.Provider>,
    );
    act(() => undefined);

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(onOpenChange).not.toHaveBeenCalled();

    view.rerender(
      <RouteSurfaceActiveContext.Provider value>
        <Dialog open onOpenChange={onOpenChange}>
          <DialogContent aria-describedby={undefined}>
            <DialogTitle>Preview</DialogTitle>
          </DialogContent>
        </Dialog>
      </RouteSurfaceActiveContext.Provider>,
    );
    expect(screen.getByRole('dialog')).toBeTruthy();
  });
});
