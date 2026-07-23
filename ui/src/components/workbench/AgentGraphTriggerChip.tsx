import { useTranslation } from 'react-i18next';
import { CalendarClock, Eye } from 'lucide-react';
import clsx from 'clsx';

import type { AgentGraphTriggerNode } from '../../lib/agentGraph';

interface AgentGraphTriggerChipProps {
  trigger: AgentGraphTriggerNode;
  faded?: boolean;
  onClick?: () => void;
  className?: string;
}

// Task/Watch trigger source — a violet chip that sits left of the session it
// fires (spec frame anu5U). Click jumps to the matching Harness definitions
// tab. Fills its parent so the canvas box + dagre layout stay in lockstep.
export const AgentGraphTriggerChip: React.FC<AgentGraphTriggerChipProps> = ({
  trigger,
  faded = false,
  onClick,
  className,
}) => {
  const { t } = useTranslation();
  const isWatch = trigger.definition_type === 'watch';
  const Icon = isWatch ? Eye : CalendarClock;
  const kindLabel = isWatch ? t('agents.graph.trigger.watch') : t('agents.graph.trigger.task');
  const name = trigger.name?.trim() || trigger.definition_id;

  return (
    <button
      type="button"
      onClick={onClick}
      title={name}
      className={clsx(
        'flex h-full w-full items-center gap-2 rounded-xl border border-violet/40 bg-violet-soft px-3 py-2 text-left transition hover:brightness-110',
        faded && 'opacity-25',
        className,
      )}
    >
      <span className="flex size-7 shrink-0 items-center justify-center rounded-lg border border-violet/30 bg-violet/[0.12] text-violet">
        <Icon className="size-3.5" />
      </span>
      <div className="flex min-w-0 flex-col">
        <span className="truncate text-[12px] font-semibold text-foreground">{name}</span>
        <span className="truncate font-mono text-[10px] uppercase tracking-wide text-violet">
          {kindLabel}
          {trigger.schedule_label && <span className="text-muted"> · {trigger.schedule_label}</span>}
        </span>
      </div>
    </button>
  );
};
