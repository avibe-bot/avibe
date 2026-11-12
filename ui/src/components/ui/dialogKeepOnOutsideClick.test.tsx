// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { pressOutside } from '@/lib/testing/pressOutside';
import { Dialog, DialogContent, DialogTitle } from './dialog';

afterEach(cleanup);

const open = (keepOnOutsideClick?: boolean) => {
  const onOpenChange = vi.fn();
  render(
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent keepOnOutsideClick={keepOnOutsideClick} closeLabel="Close">
        <DialogTitle>Form</DialogTitle>
        <input aria-label="Key" />
      </DialogContent>
    </Dialog>,
  );
  return onOpenChange;
};

describe('DialogContent keepOnOutsideClick', () => {
  it('keeps a form dialog open on a click outside, and still closes it from its close button and Escape', async () => {
    const onOpenChange = open(true);
    await pressOutside();
    expect(onOpenChange).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByLabelText('Key'), { key: 'Escape' });
    expect(onOpenChange).toHaveBeenLastCalledWith(false);
    onOpenChange.mockClear();
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(onOpenChange).toHaveBeenLastCalledWith(false);
  });

  it('leaves an ordinary dialog dismissable by a click outside', async () => {
    const onOpenChange = open();
    await pressOutside();
    expect(onOpenChange).toHaveBeenLastCalledWith(false);
  });
});
