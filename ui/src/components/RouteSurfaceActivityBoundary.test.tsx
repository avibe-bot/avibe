/* @vitest-environment jsdom */
import { useLayoutEffect, useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { createMemoryRouter, MemoryRouter, Outlet, Route, RouterProvider, Routes, useLocation, useNavigate } from 'react-router-dom';
import type { NavigateFunction } from 'react-router-dom';
import { useRouteSurfaceActive } from '../lib/routeSurfaceActivity';
import { RouteSurfaceActivityBoundary } from './RouteSurfaceActivityBoundary';

afterEach(cleanup);
for (const mode of ['declarative', 'data'] as const) {
  describe(`Route surface call-time navigation: ${mode}`, () => {
    const setup = (layoutNavigation = false) => {
      let captured!: NavigateFunction;
      let foreground!: NavigateFunction;
      const replaces = [vi.fn(), vi.fn()];
      const Consumer = () => {
        const navigate = useNavigate();
        const active = useRouteSurfaceActive();
        useLayoutEffect(() => {
          if (layoutNavigation && !active) void navigate('/layout-escape');
        }, [active, navigate]);
        useLayoutEffect(() => { captured ??= navigate; }, [navigate]);
        return null;
      };
      const Surface = () => {
        const [active, setActive] = useState(true);
        const [mounted, setMounted] = useState(true);
        const [version, setVersion] = useState(0);
        const location = useLocation();
        const navigate = useNavigate();
        useLayoutEffect(() => { foreground = navigate; }, [navigate]);
        return <>
          <button onClick={() => setActive((value) => !value)}>toggle</button>
          <button onClick={() => setMounted(false)}>retire</button>
          <button onClick={() => setVersion(1)}>callback</button>
          <output data-testid="location">{location.pathname}{location.search}{location.hash}</output>
          <output data-testid="state">{JSON.stringify(location.state)}</output>
          {mounted && <RouteSurfaceActivityBoundary active={active} inactiveReplace={replaces[version]}>
            <Outlet /><Consumer />
          </RouteSurfaceActivityBoundary>}
        </>;
      };
      const initialEntries = ['/before', '/project/item'];
      if (mode === 'data') {
        const router = createMemoryRouter([{ path: 'project/*', element: <Surface /> },
          { path: '*', element: <Surface /> }], { initialEntries });
        render(<RouterProvider router={router} />);
      } else render(<MemoryRouter initialEntries={initialEntries}>
        <Routes><Route path="project/*" element={<Surface />} /><Route path="*" element={<Surface />} /></Routes>
      </MemoryRouter>);
      return { navigate: (...args: Parameters<NavigateFunction>) => captured(...args),
        foreground: (...args: Parameters<NavigateFunction>) => foreground(...args), replaces };
    };
    it('commits inactive admission before a descendant layout effect can navigate', () => {
      setup(true);
      fireEvent.click(screen.getByText('toggle'));
      expect(screen.getByTestId('location').textContent).toBe('/project/item');
    });
    it.each(['push', 'go', 'replace'] as const)('denies captured %s after suspension and retirement, never replaying on resume', async (kind) => {
      const { navigate, replaces } = setup();
      const invoke = () => kind === 'go' ? navigate(-1) : navigate('/late-result', { replace: kind === 'replace' });
      fireEvent.click(screen.getByText('toggle'));
      fireEvent.click(screen.getByText('callback'));
      await act(async () => { await invoke(); });
      expect(screen.getByTestId('location').textContent).toBe('/project/item');
      expect(replaces[0]).not.toHaveBeenCalled();
      expect(replaces[1]).toHaveBeenCalledTimes(kind === 'replace' ? 1 : 0);
      fireEvent.click(screen.getByText('toggle'));
      expect(screen.getByTestId('location').textContent).toBe('/project/item');
      fireEvent.click(screen.getByText('retire'));
      await act(async () => { await invoke(); });
      expect(screen.getByTestId('location').textContent).toBe('/project/item');
      expect(replaces[1]).toHaveBeenCalledTimes(kind === 'replace' ? 1 : 0);
    });
    it('preserves route-relative foreground arguments and history', async () => {
      const { navigate } = setup();
      await act(async () => { await navigate('../result', { state: { relative: 'route' } }); });
      expect(screen.getByTestId('location').textContent).toBe('/result');
      expect(screen.getByTestId('state').textContent).toBe('{"relative":"route"}');
      await act(async () => { await navigate(-1); });
      expect(screen.getByTestId('location').textContent).toBe('/project/item');
    });
    it('preserves foreground relative replace/state/history and readmits a captured callback on resume', async () => {
      const { navigate, foreground } = setup();
      fireEvent.click(screen.getByText('toggle'));
      await act(async () => { await foreground('/project/settings'); });
      expect(screen.getByTestId('location').textContent).toBe('/project/settings');
      fireEvent.click(screen.getByText('toggle'));
      await act(async () => { await navigate('../next?q=1#result', { relative: 'path', replace: true, state: { source: 'retained' }, preventScrollReset: true }); });
      expect(screen.getByTestId('location').textContent).toBe('/project/next?q=1#result');
      expect(screen.getByTestId('state').textContent).toBe('{"source":"retained"}');
      await act(async () => { await navigate(-1); });
      expect(screen.getByTestId('location').textContent).toBe('/project/item');
    });
  });
}
