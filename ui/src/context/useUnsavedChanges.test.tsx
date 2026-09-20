/* @vitest-environment jsdom */

import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { RouteSurfaceActiveContext } from '../lib/routeSurfaceActivity';
import { UnsavedChangesContext } from './unsavedChangesContext';
import { useUnsavedChanges } from './useUnsavedChanges';

const DirtyProbe = () => {
  useUnsavedChanges('Unsaved editor changes');
  return null;
};

afterEach(cleanup);

describe('useUnsavedChanges', () => {
  it('withdraws a retained route registration while its surface is inactive', () => {
    const setRegistration = vi.fn();
    const context = {
      setRegistration,
      authorizeRouteAction: vi.fn(() => null),
    };
    const view = render(
      <UnsavedChangesContext.Provider value={context}>
        <RouteSurfaceActiveContext.Provider value>
          <DirtyProbe />
        </RouteSurfaceActiveContext.Provider>
      </UnsavedChangesContext.Provider>,
    );
    expect(setRegistration.mock.calls.at(-1)?.[1]).toBe('Unsaved editor changes');

    view.rerender(
      <UnsavedChangesContext.Provider value={context}>
        <RouteSurfaceActiveContext.Provider value={false}>
          <DirtyProbe />
        </RouteSurfaceActiveContext.Provider>
      </UnsavedChangesContext.Provider>,
    );
    expect(setRegistration.mock.calls.at(-1)?.[1]).toBeNull();
  });
});
