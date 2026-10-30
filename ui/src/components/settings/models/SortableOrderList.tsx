// An executor order list whose rows can be dragged by their ordinal (Models › Tools): the search, fetch and X search
// orders. The sensors, keyboard keys and announcements follow the workbench project list (SortableProjectList), so
// reordering feels the same across Avibe. Only switched-on rows move; switched-off rows stay at the end, outside the
// sortable set. A drop saves once, and the dropped order shows until that save settles.
import * as React from 'react';
import {
  DndContext, KeyboardSensor, MouseSensor, TouchSensor, closestCenter, useSensor, useSensors, type Modifier,
} from '@dnd-kit/core';
import { SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { useReducedMotion } from 'framer-motion';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';

/** What the row's drag handle (its ordinal) spreads onto itself; null for a row that does not move. */
export type OrderDragHandle = React.ComponentPropsWithRef<'button'> | null;

const verticalOnly: Modifier = ({ transform }) => ({ ...transform, x: 0 });

const SortableRow: React.FC<{
  id: string;
  /** No drag can start now (a save is out); the handle stays mounted, so a keyboard user keeps their place. */
  disabled: boolean;
  /** The row has a handle at all: a list of one has nothing to reorder. */
  handled: boolean;
  className: string;
  attrs: Record<string, string>;
  children: (handle: OrderDragHandle) => React.ReactNode;
}> = ({ id, disabled, handled, className, attrs, children }) => {
  const reducedMotion = useReducedMotion();
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition, isDragging } = useSortable({
    id,
    disabled,
    transition: reducedMotion ? null : { duration: 220, easing: 'ease' },
  });
  return (
    <li
      ref={setNodeRef}
      {...attrs}
      className={cn(className, isDragging && 'is-dragging')}
      style={{ transform: CSS.Translate.toString(transform), transition }}
    >
      {/* Disabled, the handle keeps its node and focus with no listeners and aria-disabled set. */}
      {children(handled ? { ...attributes, ...listeners, ref: setActivatorNodeRef } : null)}
    </li>
  );
};

export function SortableOrderList<Row>({
  rows, idOf, nameOf, isOn, pending, label, listRef, rowClassName, rowAttrs, onReorder, children,
}: {
  /** In the server's order, switched-off rows last. */
  rows: readonly Row[];
  idOf: (row: Row) => string;
  nameOf: (row: Row) => string;
  isOn: (row: Row) => boolean;
  /** A write for this list is out: nothing moves until the server answers it. */
  pending: boolean;
  label: string;
  listRef?: React.Ref<HTMLOListElement>;
  rowClassName: (row: Row) => string;
  rowAttrs: (row: Row) => Record<string, string>;
  /** A drop: the switched-on rows' ids before it, and the row's move within them. */
  onReorder: (shown: string[], from: number, to: number) => void;
  children: (row: Row, index: number, handle: OrderDragHandle) => React.ReactNode;
}) {
  const { t } = useTranslation();
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 300, tolerance: 8 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
      keyboardCodes: { start: ['Space'], cancel: ['Escape'], end: ['Space'] },
    }),
  );
  const on = rows.filter(isOn);
  const off = rows.filter((row) => !isOn(row));
  const served = on.map(idOf);
  // The dropped order, shown while its save is out, so the row does not jump back first. Once the server shows it,
  // or the save has been out and settled (refused, or answered with another order), the server's order is shown.
  const [dropped, setDropped] = React.useState<{ order: string[]; sent: boolean } | null>(null);
  if (dropped !== null) {
    if (dropped.order.join('\n') === served.join('\n')) setDropped(null);
    else if (pending && !dropped.sent) setDropped({ ...dropped, sent: true });
    else if (!pending && dropped.sent) setDropped(null);
  }
  const ids = dropped !== null && dropped.order.length === served.length && dropped.order.every((id) => served.includes(id)) ? dropped.order : served;
  const byId = new Map(on.map((row) => [idOf(row), row] as const));
  const name = (id: string | number) => {
    const row = byId.get(String(id));
    return row ? nameOf(row) : '';
  };
  const handled = ids.length >= 2;
  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
      modifiers={[verticalOnly]}
      accessibility={{
        screenReaderInstructions: { draggable: t('settings.models.tools.order.dragInstructions') },
        announcements: {
          onDragStart: ({ active }) => t('settings.models.tools.order.dragPickedUp', { source: name(active.id) }),
          onDragOver: ({ active, over }) => over
            ? t('settings.models.tools.order.dragPosition', { source: name(active.id), position: ids.indexOf(String(over.id)) + 1, count: ids.length })
            : undefined,
          onDragEnd: ({ active }) => t('settings.models.tools.order.dragDropped', { source: name(active.id) }),
          onDragCancel: ({ active }) => t('settings.models.tools.order.dragCancelled', { source: name(active.id) }),
        },
      }}
      onDragEnd={({ active, over }) => {
        const from = ids.indexOf(String(active.id));
        const to = ids.indexOf(String(over?.id));
        if (from < 0 || to < 0 || from === to) return;
        const next = [...ids];
        next.splice(to, 0, ...next.splice(from, 1));
        setDropped({ order: next, sent: false });
        onReorder(ids, from, to);
      }}
    >
      <SortableContext items={ids} strategy={verticalListSortingStrategy}>
        <ol ref={listRef} className="flex flex-col gap-1.5" aria-label={label}>
          {ids.map((id, index) => {
            const row = byId.get(id)!;
            return (
              <SortableRow key={id} id={id} disabled={pending || !handled} handled={handled} className={rowClassName(row)} attrs={rowAttrs(row)}>
                {(handle) => children(row, index, handle)}
              </SortableRow>
            );
          })}
          {off.map((row, index) => (
            <li key={idOf(row)} {...rowAttrs(row)} className={rowClassName(row)}>{children(row, ids.length + index, null)}</li>
          ))}
        </ol>
      </SortableContext>
    </DndContext>
  );
}

/** A row's position: its ordinal, which is also its drag handle while the row can move. */
export const OrderOrdinal: React.FC<{ index: number; off: boolean; handle: OrderDragHandle; name: string }> = ({ index, off, handle, name }) => {
  const { t } = useTranslation();
  const className = cn('model-hub-order-ordinal', index === 0 && !off && 'is-first');
  if (off) return <span className={className}>{t('settings.models.tools.order.offPosition')}</span>;
  if (!handle) return <span className={className}>{index + 1}</span>;
  return (
    <button
      type="button"
      {...handle}
      className={cn(className, 'is-handle')}
      aria-label={t('settings.models.tools.order.dragHandle', { source: name, position: index + 1 }) as string}
    >
      {index + 1}
    </button>
  );
};
