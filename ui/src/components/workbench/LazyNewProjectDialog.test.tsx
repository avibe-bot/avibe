// @vitest-environment jsdom
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('./NewProjectDialog', () => ({ NewProjectDialog: () => <div role="dialog" aria-label="new project" /> }));

import { LazyNewProjectDialog } from './LazyNewProjectDialog';

afterEach(cleanup);

// What keeps the dialog's first open from waiting: the wrapper's own preload, which the idle
// prefetch and the triggers call. The module merely being downloaded is not enough; a render
// that still suspends is held back for a visible moment.
describe('LazyNewProjectDialog', () => {
  it('draws the dialog in the render that asks for it once its preload has finished', async () => {
    await LazyNewProjectDialog.preload();
    render(<LazyNewProjectDialog onClose={() => {}} onCreated={() => {}} />);
    expect(screen.getByRole('dialog', { name: 'new project' })).toBeTruthy();
  });

  it('draws nothing in place while its code loads, then the dialog', async () => {
    vi.resetModules();
    const fresh = await import('./LazyNewProjectDialog');
    await act(async () => { render(<fresh.LazyNewProjectDialog onClose={() => {}} onCreated={() => {}} />); });
    expect(await screen.findByRole('dialog', { name: 'new project' })).toBeTruthy();
  });
});
