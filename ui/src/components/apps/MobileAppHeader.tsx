import type { ReactNode } from 'react';
import { ArrowLeft, type LucideIcon } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { hasInAppBackEntry } from '../../lib/navigationHistory';
import { Button } from '../ui/button';

/** Compact route chrome for built-in apps on phones.
 *
 * The shell owns navigation for ordinary pages, but an app surface needs the
 * whole viewport. Keeping this header local gives each app a consistent back
 * affordance without bringing the global Avibe brand bar back into the view.
 */
export const MobileAppHeader: React.FC<{
  title: string;
  icon: LucideIcon;
  actions?: ReactNode;
  fallback?: string;
}> = ({ title, icon: Icon, actions, fallback = '/' }) => {
  const navigate = useNavigate();
  const { t } = useTranslation();

  return (
    <header className="flex h-[calc(3rem+env(safe-area-inset-top))] shrink-0 items-center gap-2 border-b border-border bg-surface/95 px-3 pt-[env(safe-area-inset-top)] backdrop-blur">
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="size-8 shrink-0 text-muted"
        aria-label={t('common.back')}
        title={t('common.back')}
        onClick={() => (hasInAppBackEntry(window.history.state) ? navigate(-1) : navigate(fallback))}
      >
        <ArrowLeft className="size-4" />
      </Button>
      <div className="flex min-w-0 items-center gap-2">
        <Icon className="size-4 shrink-0 text-mint-ink" />
        <span className="truncate text-[14px] font-semibold text-foreground">{title}</span>
      </div>
      {actions && <div className="ml-auto flex shrink-0 items-center gap-1">{actions}</div>}
    </header>
  );
};
