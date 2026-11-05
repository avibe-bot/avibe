import React from 'react';
import { useTranslation } from 'react-i18next';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { formatBytes } from '@/lib/filePreview';
import { readLocalStoreDiagnostics, type LocalStoreDiagnostics as Diagnostics } from '@/lib/localStore';
import { isStandalonePwa } from '@/lib/platform';

// Settings · Diagnostics: the local message store's line (docs/plans/
// local-sync-store.md, Phase 1): whether the browser keeps it under storage
// pressure, and how much it holds. The Home Screen app asks for persistent
// storage silently; a plain browser tab is not asked (decision D5).

export type OfflineHistoryPersistence = 'granted' | 'denied' | 'not-requested' | 'unknown';

const PERSISTENCE_KEY = {
  granted: 'settings.offlineHistory.persistenceGranted',
  denied: 'settings.offlineHistory.persistenceDenied',
  'not-requested': 'settings.offlineHistory.persistenceNotRequested',
  unknown: 'settings.offlineHistory.persistenceUnknown',
} as const satisfies Record<OfflineHistoryPersistence, string>;

export function offlineHistoryPersistence(persisted: boolean | null, standalone: boolean): OfflineHistoryPersistence {
  if (persisted === true) return 'granted';
  if (persisted === false) return 'denied';
  return standalone ? 'unknown' : 'not-requested';
}

export const LocalStoreDiagnosticsView: React.FC<{ persistence: OfflineHistoryPersistence; bytes: number }> = ({
  persistence,
  bytes,
}) => {
  const { t } = useTranslation();
  return (
    <Card className="mt-6">
      <CardHeader>
        <CardTitle>{t('settings.offlineHistory.title')}</CardTitle>
        <CardDescription>{t('settings.offlineHistory.description')}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-[13px]">
        <div className="flex items-baseline justify-between gap-4">
          <span className="text-muted">{t('settings.offlineHistory.persistence')}</span>
          <span className="text-right">{t(PERSISTENCE_KEY[persistence])}</span>
        </div>
        <div className="flex items-baseline justify-between gap-4">
          <span className="text-muted">{t('settings.offlineHistory.stored')}</span>
          <span className="font-mono">{formatBytes(bytes) || '0 B'}</span>
        </div>
      </CardContent>
    </Card>
  );
};

export const LocalStoreDiagnostics: React.FC = () => {
  const [diagnostics, setDiagnostics] = React.useState<Diagnostics | null>(null);
  React.useEffect(() => {
    let alive = true;
    void readLocalStoreDiagnostics().then((next) => {
      if (alive) setDiagnostics(next);
    });
    return () => {
      alive = false;
    };
  }, []);
  // No store for this reader (none named yet, or IndexedDB unavailable): no line.
  if (!diagnostics) return null;
  return (
    <LocalStoreDiagnosticsView
      persistence={offlineHistoryPersistence(diagnostics.persisted, isStandalonePwa())}
      bytes={diagnostics.bytes}
    />
  );
};
