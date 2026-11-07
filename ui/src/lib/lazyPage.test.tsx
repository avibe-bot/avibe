// @vitest-environment jsdom
import { Component, Suspense, useState } from 'react';
import type { ReactNode } from 'react';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { lazyPage } from './lazyPage';

// Routed pages and the chat composer's editor load on demand (lazyPage). What
// the reader must not see from it: a page whose code is already here flashing
// its fallback, a page remounting (and dropping its state) when its code
// arrives, or a failed download retried in a loop instead of reported.

class Boundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? <p>failed</p> : this.props.children;
  }
}

function Counter({ label }: { label: string }) {
  const [count, setCount] = useState(0);
  return <button onClick={() => setCount((value) => value + 1)}>{`${label} ${count}`}</button>;
}

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
};

// A suspended render only retries inside an awaited act.
const mount = async (ui: ReactNode) => {
  let view!: ReturnType<typeof render>;
  await act(async () => {
    view = render(ui);
  });
  return view;
};

afterEach(cleanup);

describe('lazyPage', () => {
  it('draws a preloaded page at once, without its fallback', async () => {
    const Page = lazyPage(async () => Counter);
    await Page.preload();

    render(<Suspense fallback={<p>loading</p>}><Page label="page" /></Suspense>);

    expect(screen.getByRole('button').textContent).toBe('page 0');
    expect(screen.queryByText('loading')).toBeNull();
  });

  it('keeps the page mounted when its code arrives while it waits', async () => {
    const load = deferred<typeof Counter>();
    const Page = lazyPage(() => load.promise);
    const view = (label: string) => <Suspense fallback={<p>loading</p>}><Page label={label} /></Suspense>;
    const { rerender } = await mount(view('page'));
    expect(screen.getByText('loading')).toBeTruthy();

    await act(async () => load.resolve(Counter));
    act(() => screen.getByRole('button').click());
    rerender(view('page'));

    expect(screen.getByRole('button').textContent).toBe('page 1');
  });

  it('hands a failed download to the error boundary once, without fetching again', async () => {
    let attempts = 0;
    const failure = deferred<typeof Counter>();
    const Page = lazyPage(() => {
      attempts += 1;
      return failure.promise;
    });
    await mount(<Boundary><Suspense fallback={null}><Page label="page" /></Suspense></Boundary>);

    await act(async () => failure.reject(new Error('offline')));

    expect(screen.getByText('failed')).toBeTruthy();
    expect(attempts).toBe(1);
  });
});
