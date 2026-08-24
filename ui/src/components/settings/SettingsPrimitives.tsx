import React from 'react';
import clsx from 'clsx';
import { Search } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { Input } from '../ui/input';
import { Select } from '../ui/select';

type SettingsPanelProps = {
  title?: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
};

// Mirrors design.pen O8BNR/B6qFRA (svcSec1 / msgSec1):
// cornerRadius 12, fill --background, stroke --border 1px,
// header padding [16, 20] with bottom border, rows padding [12, 20].
export const SettingsPanel: React.FC<SettingsPanelProps> = ({
  title,
  description,
  actions,
  children,
  className,
}) => (
  <section
    className={clsx(
      'flex flex-col overflow-hidden rounded-xl border border-border bg-background',
      className
    )}
  >
    {(title || description || actions) && (
      <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
        <div className="flex min-w-0 flex-col gap-1">
          {title && <h2 className="text-[14px] font-semibold text-foreground">{title}</h2>}
          {description && (
            <p className="max-w-2xl text-[12px] leading-relaxed text-muted">{description}</p>
          )}
        </div>
        {actions && <div className="shrink-0">{actions}</div>}
      </div>
    )}
    {children}
  </section>
);

type SettingsRowProps = {
  title: React.ReactNode;
  description?: React.ReactNode;
  meta?: React.ReactNode;
  control?: React.ReactNode;
  tone?: 'default' | 'success' | 'warning' | 'info';
};

export const SettingsRow: React.FC<SettingsRowProps> = ({
  title,
  description,
  meta,
  control,
  tone = 'default',
}) => (
  <div
    className={clsx(
      'flex flex-col gap-3 border-b border-border px-5 py-3 last:border-b-0 md:flex-row md:items-center md:justify-between',
      tone === 'success' && 'bg-mint/[0.05]',
      tone === 'warning' && 'bg-gold/[0.06]',
      tone === 'info' && 'bg-cyan/[0.06]'
    )}
  >
    <div className="flex min-w-0 flex-col gap-0.5">
      <div className="text-[13px] font-medium text-foreground">{title}</div>
      {description && (
        <div className="max-w-2xl text-[11px] leading-relaxed text-muted">{description}</div>
      )}
      {meta && <div className="font-mono text-[10px] text-muted">{meta}</div>}
    </div>
    {control && <div className="shrink-0">{control}</div>}
  </div>
);

// Dense settings text field — the unified field surface (Input) at 12px.
export const CompactField: React.FC<React.InputHTMLAttributes<HTMLInputElement>> = ({
  className,
  ...props
}) => <Input className={clsx('text-[12px]', className)} {...props} />;

// Dense settings dropdown — the unified Select at 12px. The caller's className
// sizes the WRAPPER (width/margins), not the inner <select>, so layout classes
// like w-full / md:max-w-* / min-w-* / mt-1 size the control box and keep the
// absolutely-positioned chevron aligned.
export const CompactSelect: React.FC<React.SelectHTMLAttributes<HTMLSelectElement>> = ({
  className,
  ...props
}) => <Select className="text-[12px]" wrapperClassName={className} {...props} />;

type SearchFieldProps = Omit<React.InputHTMLAttributes<HTMLInputElement>, 'type'> & {
  icon?: LucideIcon;
  containerClassName?: string;
};

// Search input with leading icon — the unified field surface (Input) at 13px
// with room for the icon. (Previously a one-off bg-surface + mint-border shell.)
export const SearchField: React.FC<SearchFieldProps> = ({
  icon: Icon = Search,
  className,
  containerClassName,
  ...props
}) => (
  <div className={clsx('relative', containerClassName)}>
    <Icon
      size={14}
      className="pointer-events-none absolute left-3 top-1/2 z-10 -translate-y-1/2 text-muted"
    />
    <Input type="search" className={clsx('pl-9 pr-3 text-[13px]', className)} {...props} />
  </div>
);

// Mirrors design.pen FALFE / ylboi (mint switch on): cornerRadius 9999,
// fill --mint, blur 8 #5BFFA055 glow, 38×22 with knob inset 3.
// Off state mirrors fcMl6 (Switch/Unchecked): fill + stroke = --border-strong
// (14% white dark / 14% black light) for sufficient contrast against bg-background.
export const ToggleSwitch: React.FC<{ enabled: boolean; onClick: () => void; disabled?: boolean; label?: string }> = ({
  enabled,
  onClick,
  disabled,
  label,
}) => (
  <button
    type="button"
    role="switch"
    aria-checked={enabled}
    aria-label={label}
    disabled={disabled}
    onClick={onClick}
    className={clsx(
      'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors focus:outline-none focus:ring-2 focus:ring-mint/40 disabled:opacity-50',
      enabled
        ? 'border-mint/50 bg-mint shadow-glow-xs-mint'
        : 'border-border-strong bg-border-strong'
    )}
  >
    <span
      className={clsx(
        'inline-block size-3.5 rounded-full bg-background shadow transition-transform',
        enabled ? 'translate-x-[18px]' : 'translate-x-1'
      )}
    />
  </button>
);

type SettingsResourceRowProps = {
  icon: React.ComponentType<{ size?: number; className?: string }>;
  tileClassName: string;
  iconClassName: string;
  title: React.ReactNode;
  badges?: React.ReactNode;
  detail?: React.ReactNode;
  actions?: React.ReactNode;
  footer?: React.ReactNode;
  className?: string;
};

// Shared "managed resource" card: lead icon tile + title (+ inline badges) +
// detail line, with a right-aligned action cluster. Mirrors design.pen
// qVHh4 (VR/CM/Backends) cards; used by both the Backends and Dependencies
// settings pages so the row shell stays one concept in one home.
export const SettingsResourceRow: React.FC<SettingsResourceRowProps> = ({
  icon: Icon,
  tileClassName,
  iconClassName,
  title,
  badges,
  detail,
  actions,
  footer,
  className,
}) => (
  <div
    className={clsx(
      'flex flex-col gap-4 rounded-xl border border-border bg-background px-5 py-4 transition-colors hover:border-border-strong',
      footer ? 'md:grid md:grid-cols-[minmax(0,1fr)_auto] md:items-center' : 'md:flex-row md:items-center',
      className
    )}
  >
    <div className="flex min-w-0 flex-1 items-center gap-4">
      <div className={clsx('flex size-11 shrink-0 items-center justify-center rounded-[10px]', tileClassName)}>
        <Icon size={22} className={iconClassName} />
      </div>
      <div className="flex min-w-0 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[15px] font-semibold text-foreground">{title}</span>
          {badges}
        </div>
        {detail && <p className="text-[12px] leading-snug text-muted">{detail}</p>}
      </div>
    </div>
    {actions && <div className="flex flex-wrap items-center gap-3 md:shrink-0 md:justify-end">{actions}</div>}
    {footer && <div className="md:col-span-2">{footer}</div>}
  </div>
);
