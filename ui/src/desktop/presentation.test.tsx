// @vitest-environment jsdom
// Native guarded presentation must carry the original request token through the page's
// unsaved-work approval or cancellation without creating an uncorrelated fallback.
import { cleanup, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { UnsavedChangesProvider } from '../context/UnsavedChangesProvider';
import { useUnsavedChanges } from '../context/useUnsavedChanges';
import { DESKTOP_PRESENT_EVENT } from './bridge';
import { DesktopClientPresentation } from './useDesktopClient';

const presented = vi.hoisted(() => vi.fn((..._args: unknown[]) => true));
vi.mock('./bridge', async (original) => ({
  ...(await original<typeof import('./bridge')>()),
  presentDesktopClient: presented,
}));

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); presented.mockClear(); });

function renderDirtyPage() {
  vi.stubGlobal('__AVIBE_DESKTOP_CLIENT_VERSION__', 1);
  function Content() { useUnsavedChanges('Unsaved fixture'); return <DesktopClientPresentation />; }
  const router = createMemoryRouter([{ path: '/', element: <UnsavedChangesProvider><Content /></UnsavedChangesProvider> }]);
  render(<RouterProvider router={router} />);
}

const ask = (detail: unknown) => {
  const event = new CustomEvent(DESKTOP_PRESENT_EVENT, { cancelable: true, detail });
  window.dispatchEvent(event);
  return event;
};

it.each([
  ['approved', true, [['updates', 'native/req 1']]],
  ['refused', false, []],
] as const)('AUTH-SETUP-177 a native updates request on a dirty page, %s, is consumed and keeps its token', (_case, approve, calls) => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(approve);
  renderDirtyPage();
  const event = ask({ view: 'updates', presentationToken: 'native/req 1' });
  expect(event.defaultPrevented).toBe(true);
  expect(confirm).toHaveBeenCalledWith('Unsaved fixture');
  expect(presented.mock.calls).toEqual(calls);
});

it('AUTH-SETUP-177 drops malformed tokens rather than creating a correlated URL', () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  renderDirtyPage();
  ask({ view: 'instances' });
  ask({ view: 'updates', presentationToken: 7 });
  expect(presented.mock.calls).toEqual([['instances', undefined], ['updates', undefined]]);
});
