import { Eye, EyeOff } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { setSourceDetailsHidden, useSourceDetailsHidden } from './sourcePrivacyPreference';

export function SourcePrivateValue({ value, className }: { value: string; className?: string }) {
  const { t } = useTranslation();
  const hidden = useSourceDetailsHidden();
  return (
    <span className={cn('block min-w-0 truncate font-mono', className)} title={hidden ? undefined : value}>
      {hidden ? t('settings.models.upstream.hiddenDetails') : value}
    </span>
  );
}

export function SourcePrivacyToggle() {
  const { t } = useTranslation();
  const hidden = useSourceDetailsHidden();
  const action = t(hidden ? 'settings.models.upstream.showPrivateDetails' : 'settings.models.upstream.hidePrivateDetails');
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      className="size-6 shrink-0 text-muted"
      aria-label={action}
      title={action}
      onClick={() => setSourceDetailsHidden(!hidden)}
    >
      {hidden ? <Eye className="size-3.5" /> : <EyeOff className="size-3.5" />}
    </Button>
  );
}
