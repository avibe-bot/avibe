// @vitest-environment jsdom
// A drag keeps sliding the rows it displaces even when its list re-renders for something else in the
// same render. dnd-kit's useSortable takes a new `items` array for changed items and switches the rows'
// transition off for that render; a sidebar that re-rendered as the pointer crossed a row (the skeleton
// handover's timer) made the rows jump (the project-order e2e, CI run 42192147306).
import { useReducer, type ReactNode } from 'react';
import { useDndMonitor } from '@dnd-kit/core';
import { act, cleanup, fireEvent, render, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { WorkbenchProject } from '../context/ApiContext';
import { SortableOrderList } from '../components/settings/models/SortableOrderList';
import { SortableProjectList } from '../components/workbench/SortableProjectList';
import { useSortableItems } from './sortableItems';

it('keeps one array while the order stays the same', () => {
  const { result, rerender } = renderHook(({ ids }) => useSortableItems(ids), { initialProps: { ids: ['a', 'b'] } });
  const first = result.current;
  rerender({ ids: ['a', 'b'] });
  expect(result.current).toBe(first);
  rerender({ ids: ['b', 'a'] });
  expect(result.current).toEqual(['b', 'a']);
  expect(result.current).not.toBe(first);
});

const ROW = 40;
const names = ['Avibe', 'chainbot', 'Research', 'Documents'];

/** Re-renders whatever renders it in the render that moves the drag over a new row. */
function RerenderOnDragOver({ rerender }: { rerender: () => void }) {
  useDndMonitor({ onDragOver: rerender });
  return null;
}

/** A list of `names` whose rows carry `rowAttr`, and whose first row's content re-renders the list's
 *  owner when the drag moves over a row. */
type ListUnderTest = (props: { monitor: (index: number) => ReactNode }) => ReactNode;

const projects: WorkbenchProject[] = names.map((display_name, index) => ({
  id: `row-${index}`, scope_id: `scope-${index}`, display_name, folder_path: `/fixture/${index}`,
  created_at: '2026-01-01T00:00:00Z', last_active_at: null, archived: false,
  capabilities: { can_chat: true, has_folder: true },
}));
const ProjectList: ListUnderTest = ({ monitor }) => (
  <SortableProjectList projects={projects} disabled={false} onReorder={async () => {}}>
    {(project, handle) => (
      <><button type="button" {...handle}>{project.display_name}</button>{monitor(projects.indexOf(project))}</>
    )}
  </SortableProjectList>
);

const executors = names.map((name, index) => ({ id: `row-${index}`, name }));
const ExecutorOrder: ListUnderTest = ({ monitor }) => (
  <SortableOrderList
    rows={executors}
    idOf={(row) => row.id}
    nameOf={(row) => row.name}
    isOn={() => true}
    pending={false}
    label="Order"
    rowClassName={() => ''}
    rowAttrs={(row) => ({ 'data-row': row.id })}
    onReorder={() => {}}
  >
    {(row, index, handle) => <><button type="button" {...handle}>{row.name}</button>{monitor(index)}</>}
  </SortableOrderList>
);

describe.each([
  { list: 'the workbench project list', List: ProjectList, rowAttr: 'data-project-id' },
  { list: 'an executor order list', List: ExecutorOrder, rowAttr: 'data-row' },
])('$list', ({ List, rowAttr }) => {
  const rows = () => [...document.querySelectorAll<HTMLElement>(`[${rowAttr}]`)];
  const row = (id: string) => document.querySelector<HTMLElement>(`[${rowAttr}="${id}"]`)!;
  let styles: Map<HTMLElement, string[]>;
  let observer: MutationObserver;

  function Owner({ rerenderOnDragOver }: { rerenderOnDragOver: boolean }) {
    const [, rerender] = useReducer((n: number) => n + 1, 0);
    return <List monitor={(index) => rerenderOnDragOver && index === 0 && <RerenderOnDragOver rerender={rerender} />} />;
  }

  beforeEach(() => {
    // jsdom lays nothing out: rows stack ROW apart in document order, and a drag preview is the size of
    // the row it was picked up from (the first).
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      const sortable = this.closest(`[${rowAttr}]`);
      if (sortable) return new DOMRect(0, rows().indexOf(sortable as HTMLElement) * ROW, 200, ROW);
      if (this.closest('[data-project-drag-preview]') || this.querySelector('[data-project-drag-preview]')) {
        return new DOMRect(0, 0, 200, ROW);
      }
      return new DOMRect(0, 0, 0, 0);
    });
  });

  afterEach(() => {
    observer?.disconnect();
    cleanup();
    vi.restoreAllMocks();
  });

  /** Each style write a row has had: the value it left, which is the next write's old value. */
  function collect(records: MutationRecord[]) {
    for (const record of records) {
      const seen = styles.get(record.target as HTMLElement) ?? [];
      if (seen.length) seen[seen.length - 1] = record.oldValue ?? '';
      seen.push((record.target as HTMLElement).getAttribute('style') ?? '');
      styles.set(record.target as HTMLElement, seen);
    }
  }

  /** Picks the first row up from the keyboard and moves it down one, recording every style the rows
   *  have along the way: what the browser could have painted. */
  async function pickUpAndMoveDown() {
    const handle = row('row-0').querySelector('button')!;
    handle.focus();
    fireEvent.keyDown(handle, { code: 'Space', key: ' ' });
    // The keyboard sensor listens for the move keys from the next task on.
    await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
    styles = new Map();
    observer = new MutationObserver(collect);
    for (const sortable of rows()) observer.observe(sortable, { attributes: true, attributeFilter: ['style'], attributeOldValue: true });
    act(() => { fireEvent.keyDown(document, { code: 'ArrowDown', key: 'ArrowDown' }); });
    await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
    collect(observer.takeRecords());
  }

  for (const rerenderOnDragOver of [false, true]) {
    it(`slides the row a drag passes${rerenderOnDragOver ? ', in a render its owner also makes for something else' : ''}`, async () => {
      render(<Owner rerenderOnDragOver={rerenderOnDragOver} />);
      await pickUpAndMoveDown();
      // The drag is over the second row, which makes room by moving up into the first row's place.
      expect(row('row-1').style.transform).toBe(`translate3d(0px, -${ROW}px, 0)`);
      const displaced = (styles.get(row('row-1')) ?? []).filter((style) => style.includes(`-${ROW}px`));
      expect(displaced.length).toBeGreaterThan(0);
      // No state the row is displaced in has the transition off: it slides there.
      for (const style of displaced) expect(style).toMatch(/transition: transform 220ms ease/);
    });
  }
});
