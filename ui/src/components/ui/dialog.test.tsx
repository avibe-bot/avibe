/* @vitest-environment jsdom */

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

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
});
