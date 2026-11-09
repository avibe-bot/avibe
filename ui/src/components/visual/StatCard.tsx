import * as React from 'react';
import { cn } from '@/lib/utils';

interface StatCardProps extends React.HTMLAttributes<HTMLDivElement> {
  label: string;
  value: React.ReactNode;
  trend?: React.ReactNode;
  icon?: React.ReactNode;
}

// ui/design: models/activity/usage-24h. rounded-12, fill --background,
// border --border, padding 20, gap 6.
export const StatCard: React.FC<StatCardProps> = ({
  label,
  value,
  trend,
  icon,
  className,
  ...props
}) => (
  <div
    className={cn(
      'flex flex-col gap-1.5 rounded-xl border border-edge bg-surface px-5 py-5 shadow-elevation-0',
      className
    )}
    {...props}
  >
    <div className="flex items-center justify-between gap-2">
      <span className="text-[13px] font-medium text-muted">{label}</span>
      {icon ? <span className="text-muted [&>svg]:size-4">{icon}</span> : null}
    </div>
    <div className="text-[28px] font-bold leading-tight tracking-tight text-foreground">{value}</div>
    {trend ? <div className="text-[12px] font-medium text-muted">{trend}</div> : null}
  </div>
);
