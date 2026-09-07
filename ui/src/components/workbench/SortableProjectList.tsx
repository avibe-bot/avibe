import { useState, type ComponentPropsWithRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import {
  DndContext, DragOverlay, KeyboardSensor, MouseSensor, TouchSensor,
  closestCenter, defaultDropAnimationSideEffects, useSensor, useSensors,
  type Modifier,
} from '@dnd-kit/core';
import { SortableContext, arrayMove, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { useReducedMotion } from 'framer-motion';
import { Folder } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { WorkbenchProject } from '../../context/ApiContext';
import { orderProjects } from '../../lib/projectOrder';
import { cn } from '../../lib/utils';

export type ProjectDragHandle = ComponentPropsWithRef<'button'>;

const verticalOnly: Modifier = ({ transform }) => ({ ...transform, x: 0 });

function SortableProject({ project, disabled, children }: {
  project: WorkbenchProject;
  disabled: boolean;
  children: (handle: ProjectDragHandle) => ReactNode;
}) {
  const reducedMotion = useReducedMotion();
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition, isDragging } = useSortable({
    id: project.id,
    disabled,
    transition: reducedMotion ? null : { duration: 220, easing: 'ease' },
  });
  return (
    <div
      ref={setNodeRef}
      data-project-id={project.id}
      className={cn('project-sortable relative min-w-0', isDragging && 'opacity-0')}
      style={{ transform: CSS.Translate.toString(transform), transition }}
    >
      {children(disabled ? {} : { ...attributes, ...listeners, ref: setActivatorNodeRef })}
    </div>
  );
}

/** The existing project header is the activator on both workbench surfaces. */
export function SortableProjectList({ projects, disabled, mobile = false, onReorder, children }: {
  projects: WorkbenchProject[];
  disabled: boolean;
  mobile?: boolean;
  onReorder: (order: string[], expectedOrder: string[]) => Promise<void>;
  children: (project: WorkbenchProject, handle: ProjectDragHandle) => ReactNode;
}) {
  const { t } = useTranslation();
  const reducedMotion = useReducedMotion();
  const [activeProject, setActiveProject] = useState<WorkbenchProject | null>(null);
  const [baseline, setBaseline] = useState<string[] | null>(null);
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 300, tolerance: 8 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
      keyboardCodes: { start: ['Space'], cancel: ['Escape'], end: ['Space'] },
    }),
  );
  const list = baseline ? orderProjects(projects, baseline) : projects;
  const ids = list.map((project) => project.id);
  const name = (id: string | number) => projects.find((project) => project.id === id)?.display_name ?? '';
  const finish = () => { setActiveProject(null); setBaseline(null); };

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
      modifiers={[verticalOnly]}
      accessibility={{
        screenReaderInstructions: { draggable: t('projects.reorderInstructions') },
        announcements: {
          onDragStart: ({ active }) => t('projects.reorderPickedUp', { name: name(active.id) }),
          onDragOver: ({ active, over }) => over
            ? t('projects.reorderPosition', { name: name(active.id), position: ids.indexOf(String(over.id)) + 1, count: ids.length })
            : undefined,
          onDragEnd: ({ active }) => t('projects.reorderDropped', { name: name(active.id) }),
          onDragCancel: () => t('projects.reorderCancelled'),
        },
      }}
      onDragStart={({ active }) => {
        setBaseline(ids);
        setActiveProject(projects.find((project) => project.id === active.id) ?? null);
      }}
      onDragCancel={finish}
      onDragEnd={({ active, over }) => {
        const expected = baseline ?? ids;
        const from = expected.indexOf(String(active.id));
        const to = expected.indexOf(String(over?.id));
        finish();
        if (from >= 0 && to >= 0 && from !== to) void onReorder(arrayMove(expected, from, to), expected);
      }}
    >
      <SortableContext items={ids} strategy={verticalListSortingStrategy}>
        {list.map((project) => (
          <SortableProject key={project.id} project={project} disabled={disabled || list.length < 2}>
            {(handle) => children(project, handle)}
          </SortableProject>
        ))}
      </SortableContext>
      {createPortal(
        <DragOverlay
          transition={reducedMotion ? '' : undefined}
          dropAnimation={reducedMotion ? null : {
            duration: 220,
            easing: 'ease',
            sideEffects: defaultDropAnimationSideEffects({ styles: { active: { opacity: '0' } } }),
          }}
        >
          {activeProject && (
            <div
              data-project-drag-preview
              className={cn(
                'project-drag-preview flex cursor-grabbing items-center gap-2 rounded-md border border-border-strong bg-surface-2 text-foreground shadow-lg',
                mobile ? 'px-4 py-3.5 text-sm font-semibold' : 'px-2 py-1.5 text-xs font-medium',
              )}
            >
              <Folder className={cn('shrink-0 text-cyan-ink', mobile ? 'size-4' : 'size-3.5')} />
              <span className="min-w-0 truncate">{activeProject.display_name}</span>
            </div>
          )}
        </DragOverlay>,
        document.body,
      )}
    </DndContext>
  );
}
