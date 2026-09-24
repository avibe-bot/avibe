import { useSyncExternalStore } from 'react';
import { Eye, EyeOff } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

const STORAGE_KEY = 'avibe.model-hub.hide-accounts.v1';
const listeners = new Set<() => void>();
let hiddenInMemory: boolean | undefined;

const getHidden = () => {
  if (hiddenInMemory !== undefined) return hiddenInMemory;
  try {
    return localStorage.getItem(STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
};

const subscribe = (listener: () => void) => {
  listeners.add(listener);
  const onStorage = (event: StorageEvent) => {
    if (event.key === STORAGE_KEY || event.key === null) {
      hiddenInMemory = undefined;
      listener();
    }
  };
  window.addEventListener('storage', onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener('storage', onStorage);
  };
};

const setHidden = (hidden: boolean) => {
  try {
    localStorage.setItem(STORAGE_KEY, String(hidden));
    hiddenInMemory = undefined;
  } catch {
    // Browser storage can be disabled; visibility still works for this page.
    hiddenInMemory = hidden;
  }
  listeners.forEach((listener) => listener());
};

export function SourceAccountLabel({ label, className }: { label: string; className?: string }) {
  const { t } = useTranslation();
  const hidden = useSyncExternalStore(subscribe, getHidden, () => true);
  const action = t(hidden ? 'settings.models.upstream.showAccount' : 'settings.models.upstream.hideAccount');
  return (
    <span className={cn('flex min-w-0 items-center gap-1', className)} data-source-account>
      <span className="min-w-0 truncate font-mono" title={hidden ? undefined : label}>
        {hidden ? t('settings.models.upstream.hiddenAccount') : label}
      </span>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="pointer-events-auto relative z-10 size-6 shrink-0 text-muted"
        aria-label={action}
        aria-pressed={!hidden}
        onClick={(event) => {
          event.stopPropagation();
          setHidden(!hidden);
        }}
      >
        {hidden ? <Eye className="size-3.5" /> : <EyeOff className="size-3.5" />}
      </Button>
    </span>
  );
}
