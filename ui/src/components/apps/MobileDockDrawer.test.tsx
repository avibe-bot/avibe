/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock('../../context/DockContext', () => ({
  useDock: () => ({ order: [], pins: [], undock: vi.fn(), unpin: vi.fn() }),
}));
vi.mock('../../lib/useAuthAccount', () => ({ useAuthAccount: () => ({ email: null }) }));
vi.mock('../useShowPages', () => ({ useShowPageInventory: () => ({ pages: [] }) }));
vi.mock('../workbench/MorePage', () => ({
  MoreAccountSection: () => null,
  MoreAppearanceSection: () => null,
  MoreConnectionSection: () => null,
}));

import { MobileDockDrawer } from './MobileDockDrawer';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('MobileDockDrawer settings entry', () => {
  it('opens Settings on General, the same place a desktop entry lands', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();

    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route path="/" element={<MobileDockDrawer open onClose={onClose} />} />
          <Route path="/settings" element={<div>section-list</div>} />
          <Route path="/settings/general" element={<div>general-page</div>} />
        </Routes>
      </MemoryRouter>,
    );

    const chip = screen.getByRole('link', { name: 'more.controlPanel' });
    expect(chip.getAttribute('href')).toBe('/settings/general');

    await user.click(chip);

    // A phone's ordinary way into Settings is a destination, not a menu: the
    // section list stays reachable from General's back row, but it is not what
    // tapping 设置 hands the user.
    expect(await screen.findByText('general-page')).toBeTruthy();
    expect(screen.queryByText('section-list')).toBeNull();
    expect(onClose).toHaveBeenCalled();
  });
});
