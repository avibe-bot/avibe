/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';

import { ShowPageRoute } from './ShowPageRoute';

const api = vi.hoisted(() => ({
  getSession: vi.fn(),
  getShowPages: vi.fn(),
  connectWorkbenchEvents: vi.fn(),
}));

vi.mock('../../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../../context/DockContext', () => ({ useDock: () => ({ unpin: vi.fn() }) }));
vi.mock('../../context/WindowManagerContext', () => ({
  useWindowManager: () => ({ windows: [], openApp: vi.fn(), focus: vi.fn(), restore: vi.fn() }),
}));
vi.mock('../../context/showPageDrag', () => ({
  useShowPageDrag: () => ({ active: false, begin: vi.fn(), end: vi.fn(), dropToDock: vi.fn() }),
}));
vi.mock('../../lib/useIsDesktop', () => ({ useIsDesktop: () => false }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

const LocationProbe = () => {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}{location.search}</output>;
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('mobile Show Page app route', () => {
  it('uses one full-width header with back, annotation, chat, and share controls', async () => {
    api.getSession.mockResolvedValue({ id: 'session-1', status: 'active', title: 'Release dashboard' });
    api.getShowPages.mockResolvedValue({ pages: [] });
    api.connectWorkbenchEvents.mockReturnValue(vi.fn());

    render(
      <MemoryRouter initialEntries={['/apps/show/session-1']}>
        <Routes>
          <Route path="/apps/show/:sessionId" element={<ShowPageRoute />} />
          <Route path="/chat/:sessionId" element={null} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>,
    );

    expect(await screen.findByText('Release dashboard')).toBeTruthy();
    const header = screen.getByRole('banner');
    expect(header.querySelectorAll('button')).toHaveLength(4);
    expect(header.querySelector('a')).toBeNull();
    expect(screen.getByRole('button', { name: 'common.back' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'chat.showPage.annotate.unavailable' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'chat.showPage.backToChat' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'chat.showPage.share' })).toBeTruthy();

    const surface = header.parentElement;
    expect(surface?.className).toContain('h-full');
    expect(surface?.className).toContain('w-full');
    expect(surface?.className).toContain('pb-[env(safe-area-inset-bottom)]');
    expect(surface?.className).not.toContain('rounded');
    expect(surface?.className).not.toContain('border border-border');
    expect(screen.getByTitle('chat.showPage.title').getAttribute('src')).toBe('/show/session-1/?vibe-embed=1');

    fireEvent.click(screen.getByRole('button', { name: 'chat.showPage.backToChat' }));
    expect(screen.getByTestId('location').textContent).toBe('/chat/session-1?view=chat');
  });

  it('keeps action controls out of the missing-session placeholder', async () => {
    api.getSession.mockResolvedValue(null);

    render(
      <MemoryRouter initialEntries={['/apps/show/session-1']}>
        <Routes>
          <Route path="/apps/show/:sessionId" element={<ShowPageRoute />} />
        </Routes>
      </MemoryRouter>,
    );

    const header = await screen.findByRole('banner');
    expect(await screen.findByText('apps.showPage.missingTitle')).toBeTruthy();
    expect(header.querySelectorAll('button')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'common.back' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'chat.showPage.annotate.unavailable' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'chat.showPage.backToChat' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'chat.showPage.share' })).toBeNull();
  });
});
