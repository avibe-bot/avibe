import React from 'react';
import clsx from 'clsx';

type SettingsTab = string;

export type SettingsPageShellProps = {
  title: string;
  subtitle: string;
  activeTab: SettingsTab;
  breadcrumb?: React.ReactNode;
  actions?: React.ReactNode;
  children: React.ReactNode;
  /**
   * `section` (default) is the 28/700 heading every Settings section has
   * shipped with. `landing` is the quieter block the source draws for the
   * Settings landing (design.pen `Ozmrf`: 27/600 over a 13 `$--muted` line).
   * Opt-in, so adding it restyles no sibling section.
   */
  titleScale?: 'section' | 'landing';
};

export const SettingsPageShell: React.FC<SettingsPageShellProps> = ({
  title,
  subtitle,
  activeTab,
  breadcrumb,
  actions,
  children,
  titleScale = 'section',
}) => {
  void activeTab;
  const landing = titleScale === 'landing';

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-1.5">
          <h1
            className={clsx(
              'leading-tight tracking-[-0.4px] text-foreground',
              landing ? 'text-[27px] font-semibold' : 'text-[28px] font-bold',
            )}
          >
            {title}
          </h1>
          <p className={clsx('max-w-3xl leading-[1.55] text-muted', landing ? 'text-[13px]' : 'text-[14px]')}>
            {subtitle}
          </p>
        </div>
        {actions && <div className="shrink-0">{actions}</div>}
      </div>

      {/* Provider/platform detail remains within the active Settings section. */}
      {breadcrumb && <div className="hidden font-mono text-[11px] text-muted md:block">{breadcrumb}</div>}

      <div className="flex flex-col gap-4">{children}</div>
    </div>
  );
};
