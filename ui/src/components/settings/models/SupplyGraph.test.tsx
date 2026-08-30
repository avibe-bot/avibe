// @vitest-environment jsdom
import * as React from 'react';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SupplyGraph } from './SupplyGraph';
import type { SupplyRelation } from './supplyRelations';

const relation: SupplyRelation = { sourceId: 'src_a', backend: 'claude', kind: 'gateway' };
const secondRelation: SupplyRelation = { sourceId: 'src_b', backend: 'claude', kind: 'connected_unused' };
const codexRelation: SupplyRelation = { sourceId: 'src_a', backend: 'codex', kind: 'gateway' };

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
      <div data-agent-backend="codex" />
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
    expect(svg?.querySelector('.model-hub-rail-line')).toBeNull();
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
    const anchorCoordinates = () => Array.from(view.container.querySelectorAll<SVGCircleElement>('.model-hub-wire-node--shared-anchor'))
      .map((anchor) => `${anchor.getAttribute('cx')}:${anchor.getAttribute('cy')}`)
      .sort();
    expect(anchorCoordinates()).toEqual(['30:15', '30:35', '90:50']);
    expect(view.container.querySelectorAll('.model-hub-wire-node--gateway, .model-hub-wire-node--connected_unused')).toHaveLength(0);

    view.rerender(<Fixture relations={[secondRelation, relation]} />);
    await waitFor(() => expect(anchorCoordinates()).toEqual(['30:15', '30:35', '90:50']));
    expect(view.container.querySelectorAll('.model-hub-wire-node--gateway, .model-hub-wire-node--connected_unused')).toHaveLength(0);
  });

  it('highlights only relations connected to the hovered or focused endpoint', async () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function bounds() {
      if (this.dataset.testid === 'graph-root') return rect(0, 0, 120, 120);
      if (this.dataset.sourceId === 'src_a') return rect(10, 10, 20, 10);
      if (this.dataset.sourceId === 'src_b') return rect(10, 30, 20, 10);
      if (this.dataset.agentBackend === 'claude') return rect(90, 40, 20, 20);
      if (this.dataset.agentBackend === 'codex') return rect(90, 80, 20, 20);
      return rect(0, 0, 0, 0);
    });

    const view = render(<Fixture relations={[relation, secondRelation, codexRelation]} />);
    const paths = await waitFor(() => {
      const elements = Array.from(view.container.querySelectorAll<SVGPathElement>('.model-hub-wire'));
      expect(elements).toHaveLength(3);
      return elements;
    });
    const highlighted = () => paths.map((path) => path.classList.contains('model-hub-wire--highlighted'));
    expect(highlighted()).toEqual([false, false, false]);

    fireEvent.pointerOver(view.container.querySelector('[data-source-id="src_a"]') as Element);
    await waitFor(() => expect(highlighted()).toEqual([true, false, true]));

    fireEvent.pointerOut(view.container.querySelector('[data-source-id="src_a"]') as Element, { relatedTarget: view.container });
    await waitFor(() => expect(highlighted()).toEqual([false, false, false]));

    fireEvent.focusIn(view.container.querySelector('[data-agent-backend="claude"]') as Element);
    await waitFor(() => expect(highlighted()).toEqual([true, true, false]));
    fireEvent.focusOut(view.container.querySelector('[data-agent-backend="claude"]') as Element, { relatedTarget: view.container });
    await waitFor(() => expect(highlighted()).toEqual([false, false, false]));
  });
});
