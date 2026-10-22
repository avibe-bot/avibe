/* @vitest-environment jsdom */

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useState } from 'react';

import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from './dialog';

afterEach(cleanup);

describe('DialogContent focus lifecycle', () => {
  it('restores focus to an underlying opener when a nested dialog closes', async () => {
    const user = userEvent.setup();
    const Harness = () => {
      const [parentOpen, setParentOpen] = useState(false);
      const [childOpen, setChildOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setParentOpen(true)}>open-parent</button>
          <Dialog open={parentOpen} onOpenChange={setParentOpen}>
            <DialogContent aria-describedby={undefined}>
              <DialogTitle>Parent</DialogTitle>
              <button type="button" onClick={() => setChildOpen(true)}>open-child</button>
            </DialogContent>
          </Dialog>
          <Dialog open={childOpen} onOpenChange={setChildOpen}>
            <DialogContent aria-describedby={undefined}>
              <DialogTitle>Child</DialogTitle>
            </DialogContent>
          </Dialog>
        </>
      );
    };

    render(<Harness />);
    await user.click(screen.getByRole('button', { name: 'open-parent' }));
    const childTrigger = screen.getByRole('button', { name: 'open-child' });
    await user.click(childTrigger);
    await user.keyboard('{Escape}');

    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Child' })).toBeNull());
    expect(document.activeElement).toBe(childTrigger);
  });

  it('leaves focus with a replacement nested dialog', async () => {
    const user = userEvent.setup();
    const Harness = () => {
      const [childOpen, setChildOpen] = useState(false);
      const [replacementOpen, setReplacementOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setChildOpen(true)}>open-child</button>
          <Dialog open={childOpen} onOpenChange={(next) => {
            setChildOpen(next);
            if (!next) setReplacementOpen(true);
          }}>
            <DialogContent aria-describedby={undefined}>
              <DialogTitle>Child</DialogTitle>
            </DialogContent>
          </Dialog>
          <Dialog open={replacementOpen} onOpenChange={setReplacementOpen}>
            <DialogContent aria-describedby={undefined}>
              <DialogTitle>Replacement</DialogTitle>
              <button type="button">replacement-action</button>
            </DialogContent>
          </Dialog>
        </>
      );
    };

    render(<Harness />);
    const childTrigger = screen.getByRole('button', { name: 'open-child' });
    await user.click(childTrigger);
    await user.keyboard('{Escape}');

    const replacement = await screen.findByRole('dialog', { name: 'Replacement' });
    expect(replacement.contains(document.activeElement)).toBe(true);
  });
});

describe('DialogContent layout safety', () => {
  it('wraps long unbroken titles without letting modal controls overflow', () => {
    const title = 'Delete AVIBE_UI_REGRESSION_STANDARD_20260810_1234567890_abcdefghijklmnopqrstuvwxyz';
    render(
      <Dialog open>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>{title}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <button type="button">Cancel</button>
            <button type="button">Delete</button>
          </DialogFooter>
        </DialogContent>
      </Dialog>,
    );

    const dialog = screen.getByRole('dialog');
    const heading = screen.getByRole('heading', { name: title });
    expect(dialog.className).toContain('min-w-0');
    expect(heading.className).toContain('min-w-0');
    expect(heading.className).toContain('break-words');
    expect(heading.className).toContain('[overflow-wrap:anywhere]');
    expect(heading.className).toContain('pr-6');
    expect(screen.getByRole('button', { name: 'Delete' })).toBeTruthy();
  });
});

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
