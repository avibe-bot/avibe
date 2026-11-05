import React from 'react';
import { useTranslation } from 'react-i18next';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { formatBytes } from '@/lib/filePreview';
import {
  readLocalStoreDiagnostics,
  subscribeLocalStoreDiagnostics,
  type LocalStoreDiagnostics as Diagnostics,
} from '@/lib/localStore';
import { getLocalSnapshotPrincipal, subscribeLocalSnapshotPrincipal } from '@/lib/localSnapshots';
import { isStandalonePwa } from '@/lib/platform';
import { offlineHistoryPersistence, type OfflineHistoryPersistence } from './offlineHistoryPersistence';

// Settings · Diagnostics: the local message store's line (docs/plans/
// local-sync-store.md, Phase 1): whether the browser keeps it under storage
// pressure, and how much it holds. The Home Screen app asks for persistent
// storage silently; a plain browser tab is not asked (decision D5).

const PERSISTENCE_KEY = {
  granted: 'settings.offlineHistory.persistenceGranted',
  denied: 'settings.offlineHistory.persistenceDenied',
  'not-requested': 'settings.offlineHistory.persistenceNotRequested',
  unknown: 'settings.offlineHistory.persistenceUnknown',
} as const satisfies Record<OfflineHistoryPersistence, string>;

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
          <span className="font-mono">{formatBytes(bytes) || t('settings.offlineHistory.storedNone')}</span>
        </div>
      </CardContent>
    </Card>
  );
};

export const LocalStoreDiagnostics: React.FC = () => {
  // The store answers only for a named reader, and a page loaded straight here
  // names one in an effect that runs after this one: read again when it does.
  const reader = React.useSyncExternalStore(subscribeLocalSnapshotPrincipal, getLocalSnapshotPrincipal);
  // And when the installed app's persistence request is answered, which can be
  // after the page read the store.
  const [answered, setAnswered] = React.useState(0);
  React.useEffect(() => subscribeLocalStoreDiagnostics(() => setAnswered((count) => count + 1)), []);
  const [diagnostics, setDiagnostics] = React.useState<Diagnostics | null>(null);
  React.useEffect(() => {
    let alive = true;
    void readLocalStoreDiagnostics().then((next) => {
      if (alive) setDiagnostics(next);
    });
    return () => {
      alive = false;
    };
  }, [reader, answered]);
  // No store for this reader (none named yet, or IndexedDB unavailable): no line.
  if (!diagnostics) return null;
  return (
    <LocalStoreDiagnosticsView
      persistence={offlineHistoryPersistence(diagnostics.persisted, isStandalonePwa())}
      bytes={diagnostics.bytes}
    />
  );
};
