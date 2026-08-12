// @vitest-environment jsdom
import * as React from 'react';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SupplyGraph } from './SupplyGraph';
import type { SupplyRelation } from './supplyRelations';

const relation: SupplyRelation = { sourceId: 'src_a', backend: 'claude', kind: 'gateway' };

const rect = (left: number, top: number, width: number, height: number): DOMRect => ({
  x: left,
  y: top,
  left,
  top,
  right: left + width,
  bottom: top + height,
  width,
  height,
  toJSON: () => ({}),
});

const Fixture: React.FC = () => {
  const ref = React.useRef<HTMLDivElement>(null);
  const [mounted, setMounted] = React.useState(false);
  React.useLayoutEffect(() => setMounted(true), []);
  return (
    <div ref={ref} data-testid="graph-root">
      <div data-source-id="src_a" />
      <div data-agent-backend="claude" />
      {mounted && <SupplyGraph containerRef={ref} relations={[relation]} />}
    </div>
  );
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('SupplyGraph', () => {
  it('re-measures wires when a nested card scrolls without resizing the shell', async () => {
    let sourceTop = 10;
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function bounds() {
      if (this.dataset.testid === 'graph-root') return rect(0, 0, 120, 100);
      if (this.dataset.sourceId === 'src_a') return rect(10, sourceTop, 20, 10);
      if (this.dataset.agentBackend === 'claude') return rect(90, 40, 20, 20);
      return rect(0, 0, 0, 0);
    });

    const view = render(<Fixture />);
    const path = await waitFor(() => {
      const element = view.container.querySelector<SVGPathElement>('.model-hub-wire');
      expect(element).not.toBeNull();
      return element as SVGPathElement;
    });
    expect(path.getAttribute('d')).toContain('M 30 15');

    sourceTop = 30;
    fireEvent.scroll(view.container.querySelector('[data-source-id="src_a"]') as Element);

    await waitFor(() => expect(path.getAttribute('d')).toContain('M 30 35'));
  });
});
