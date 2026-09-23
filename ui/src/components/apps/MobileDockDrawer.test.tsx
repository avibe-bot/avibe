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
  it('opens Settings on the section list a phone has to navigate from', async () => {
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
    expect(chip.getAttribute('href')).toBe('/settings');

    await user.click(chip);

    // A phone shows one Settings screen at a time, so landing inside a section
    // hides every other one behind a Back the user has to find first. The menu
    // is the screen that names them all, and it is what tapping 设置 hands over;
    // the rail a desktop keeps beside the page is what excuses it there.
    expect(await screen.findByText('section-list')).toBeTruthy();
    expect(screen.queryByText('general-page')).toBeNull();
    expect(onClose).toHaveBeenCalled();
  });
});
