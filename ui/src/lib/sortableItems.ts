import { useMemo } from 'react';

/** The `items` for a dnd-kit `SortableContext`: one array for as long as the order stays the same.
 *  `useSortable` compares `items` by identity and takes a new array for changed items, and for that
 *  render it switches the rows' transition off. A list that maps its ids afresh on every render would
 *  then let any re-render during a drag (a refresh, a timer) land in the render that moves the rows
 *  under the pointer, and they jump instead of sliding. */
export function useSortableItems(ids: readonly string[]): string[] {
  const key = JSON.stringify(ids);
  return useMemo(() => JSON.parse(key) as string[], [key]);
}
