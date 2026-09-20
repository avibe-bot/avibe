/* @vitest-environment jsdom */

import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  RouteSurfaceActiveContext,
  useRouteSurfaceWindowEvent,
} from './routeSurfaceActivity';

const KeyProbe = ({ onKey }: { onKey: () => void }) => {
  useRouteSurfaceWindowEvent('keydown', onKey);
  return null;
};

afterEach(cleanup);

describe('route surface window events', () => {
  it('withdraws global event ownership while the route is retained but inactive', () => {
    const onKey = vi.fn();
    const view = render(
      <RouteSurfaceActiveContext.Provider value>
        <KeyProbe onKey={onKey} />
      </RouteSurfaceActiveContext.Provider>,
    );

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 's' }));
    expect(onKey).toHaveBeenCalledTimes(1);

    view.rerender(
      <RouteSurfaceActiveContext.Provider value={false}>
        <KeyProbe onKey={onKey} />
      </RouteSurfaceActiveContext.Provider>,
    );
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 's' }));
    expect(onKey).toHaveBeenCalledTimes(1);

    view.rerender(
      <RouteSurfaceActiveContext.Provider value>
        <KeyProbe onKey={onKey} />
      </RouteSurfaceActiveContext.Provider>,
    );
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 's' }));
    expect(onKey).toHaveBeenCalledTimes(2);
  });
});
