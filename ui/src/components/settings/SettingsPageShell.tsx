import React from 'react';

type SettingsTab = string;

export type SettingsPageShellProps = {
  title: string;
  subtitle: string;
  activeTab: SettingsTab;
  breadcrumb?: React.ReactNode;
  actions?: React.ReactNode;
  children: React.ReactNode;
};

export const SettingsPageShell: React.FC<SettingsPageShellProps> = ({
  title,
  subtitle,
  activeTab,
  breadcrumb,
  actions,
  children,
}) => {
  void activeTab;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-1.5">
          <h1 className="text-[28px] font-bold leading-tight tracking-[-0.4px] text-foreground">{title}</h1>
          <p className="max-w-3xl text-[14px] leading-[1.55] text-muted">{subtitle}</p>
        </div>
        {actions && <div className="shrink-0">{actions}</div>}
      </div>

      {/* Provider/platform detail remains within the active Settings section. */}
      {breadcrumb && <div className="hidden font-mono text-[11px] text-muted md:block">{breadcrumb}</div>}

      <div className="flex flex-col gap-4">{children}</div>
    </div>
  );
};
