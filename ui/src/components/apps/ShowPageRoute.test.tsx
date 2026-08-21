/* @vitest-environment jsdom */

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import { ShowPageRoute } from './ShowPageRoute';

const api = vi.hoisted(() => ({
  getSession: vi.fn(),
}));

vi.mock('../../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../../context/DockContext', () => ({ useDock: () => ({ unpin: vi.fn() }) }));
vi.mock('../../context/WindowManagerContext', () => ({
  useWindowManager: () => ({ windows: [], openApp: vi.fn(), focus: vi.fn(), restore: vi.fn() }),
}));
vi.mock('../../lib/useIsDesktop', () => ({ useIsDesktop: () => false }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('mobile Show Page app route', () => {
  it('uses one full-width header with only back, title, and annotation controls', async () => {
    api.getSession.mockResolvedValue({ id: 'session-1', status: 'active', title: 'Release dashboard' });

    render(
      <MemoryRouter initialEntries={['/apps/show/session-1']}>
        <Routes>
          <Route path="/apps/show/:sessionId" element={<ShowPageRoute />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(await screen.findByText('Release dashboard')).toBeTruthy();
    const header = screen.getByRole('banner');
    expect(header.querySelectorAll('button')).toHaveLength(2);
    expect(header.querySelector('a')).toBeNull();
    expect(screen.getByRole('button', { name: 'common.back' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'chat.showPage.annotate.unavailable' })).toBeTruthy();

    const surface = header.parentElement;
    expect(surface?.className).toContain('h-full');
    expect(surface?.className).toContain('w-full');
    expect(surface?.className).not.toContain('rounded');
    expect(surface?.className).not.toContain('border border-border');
    expect(screen.getByTitle('chat.showPage.title').getAttribute('src')).toBe('/show/session-1/');
  });
});
