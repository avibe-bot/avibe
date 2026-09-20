import React from 'react';
import { Check, ChevronDown, ChevronUp } from 'lucide-react';
import clsx from 'clsx';

interface StepShellProps {
  active: boolean;
  children: React.ReactNode;
}

export const StepShell: React.FC<StepShellProps> = ({ active, children }) => (
  <div
    className={clsx(
      'overflow-hidden rounded-xl border transition-colors',
      active
        ? 'border-mint/35 bg-surface-2 shadow-mint-card-sm'
        : 'border-border bg-background'
    )}
  >
    {children}
  </div>
);

interface StepHeaderProps {
  step: number;
  title: string;
  icon: React.ReactNode;
  completed?: boolean;
  expanded: boolean;
  onToggle: () => void;
}

export const StepHeader: React.FC<StepHeaderProps> = ({
  step,
  title,
  icon,
  completed,
  expanded,
  onToggle,
}) => (
  <button
    onClick={onToggle}
    className="flex w-full items-center justify-between gap-3 px-5 py-4 text-left transition-colors hover:bg-foreground/[0.02]"
  >
    <div className="flex items-center gap-3">
      <span
        className={clsx(
          'flex size-7 items-center justify-center rounded-full text-[12px] font-bold transition-colors',
          completed ? 'bg-mint text-primary-foreground' : 'bg-cyan/15 text-cyan-ink'
        )}
      >
        {completed ? <Check size={14} /> : step}
      </span>
      <span className="flex items-center gap-2 text-[14px] font-semibold text-foreground">
        {icon}
        {title}
      </span>
    </div>
    {expanded ? <ChevronUp size={18} className="text-muted" /> : <ChevronDown size={18} className="text-muted" />}
  </button>
);
