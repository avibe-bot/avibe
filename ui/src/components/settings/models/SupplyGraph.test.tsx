// @vitest-environment jsdom
import * as React from 'react';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SupplyGraph } from './SupplyGraph';
import type { SupplyRelation } from './supplyRelations';

const relation: SupplyRelation = { sourceId: 'src_a', backend: 'claude', kind: 'gateway' };
const secondRelation: SupplyRelation = { sourceId: 'src_b', backend: 'claude', kind: 'connected_unused' };

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

const Fixture: React.FC<{ relations?: SupplyRelation[] }> = ({ relations = [relation] }) => {
  const ref = React.useRef<HTMLDivElement>(null);
  return (
    <div ref={ref} data-testid="graph-root">
      <div data-source-id="src_a" />
      <div data-source-id="src_b" />
      <div data-agent-backend="claude" />
      <SupplyGraph containerRef={ref} relations={relations} />
    </div>
  );
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('SupplyGraph', () => {
  it('draws on the shared mount and re-measures when a nested card scrolls', async () => {
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
    const svg = view.container.querySelector('svg');
    expect(svg?.classList.contains('overflow-hidden')).toBe(true);
    expect(svg?.classList.contains('overflow-visible')).toBe(false);
    expect(path.getAttribute('d')).toContain('M 30 15');

    sourceTop = 30;
    fireEvent.scroll(view.container.querySelector('[data-source-id="src_a"]') as Element);

    await waitFor(() => expect(path.getAttribute('d')).toContain('M 30 35'));
  });

  it('lands every relation for an agent on the agent card midpoint', async () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function bounds() {
      if (this.dataset.testid === 'graph-root') return rect(0, 0, 120, 100);
      if (this.dataset.sourceId === 'src_a') return rect(10, 10, 20, 10);
      if (this.dataset.sourceId === 'src_b') return rect(10, 30, 20, 10);
      if (this.dataset.agentBackend === 'claude') return rect(90, 40, 20, 20);
      return rect(0, 0, 0, 0);
    });

    const view = render(<Fixture relations={[relation, secondRelation]} />);
    const paths = await waitFor(() => {
      const elements = Array.from(view.container.querySelectorAll<SVGPathElement>('.model-hub-wire'));
      expect(elements).toHaveLength(2);
      return elements;
    });

    expect(paths.map((path) => path.getAttribute('d'))).toEqual([
      'M 30 15 C 60 15, 60 50, 90 50',
      'M 30 35 C 60 35, 60 50, 90 50',
    ]);
  });
});
