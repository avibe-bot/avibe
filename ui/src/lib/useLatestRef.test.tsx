import { useState } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { useLatestRef } from './useLatestRef';

describe('useLatestRef', () => {
  it('exposes the value of the current render, not of the previous one', () => {
    // A render-phase state update makes React re-render this component
    // immediately, before anything commits. The second pass is where the two
    // possible implementations diverge: writing during render reports 1,
    // deferring the write to an effect would still report the initial 0,
    // because no effect has run yet. Every caller reads the ref from a
    // listener or effect that fires after this point, so "already current in
    // the render that produced the new value" is the property they rely on.
    function Probe() {
      const [n, setN] = useState(0);
      const latest = useLatestRef(n);
      if (n === 0) setN(1);
      return <span>{`n=${n} ref=${latest.current}`}</span>;
    }

    expect(renderToStaticMarkup(<Probe />)).toBe('<span>n=1 ref=1</span>');
  });

  it('starts at the first value handed to it', () => {
    function Probe({ label }: { label: string }) {
      const latest = useLatestRef(label);
      return <span>{latest.current}</span>;
    }

    expect(renderToStaticMarkup(<Probe label="initial" />)).toBe('<span>initial</span>');
  });

  it('tracks a value that is not a string, by identity', () => {
    const first = { id: 1 };
    const second = { id: 2 };

    function Probe() {
      const [current, setCurrent] = useState(first);
      const latest = useLatestRef(current);
      if (current === first) setCurrent(second);
      return <span>{latest.current === second ? 'second' : 'stale'}</span>;
    }

    expect(renderToStaticMarkup(<Probe />)).toBe('<span>second</span>');
  });
});
