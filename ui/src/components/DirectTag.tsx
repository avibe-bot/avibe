import { useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';

import { liveStreamVia, subscribeLiveStreamVia } from '../lib/liveStreamVia';

const unknown = () => null;

// A small tag beside the instance name while this page's live stream runs through the instance's
// direct ingress, as that stream itself reported. Any other way in shows nothing: it names no
// vendor and counts nothing (ui/design: mobile-app/direct-tag).
export const DirectTag: React.FC = () => {
  const { t } = useTranslation();
  const via = useSyncExternalStore(subscribeLiveStreamVia, liveStreamVia, unknown);
  if (via !== 'direct') return null;
  return (
    <span className="shrink-0 rounded-full border border-mint/30 bg-mint-soft px-1.5 text-2xs font-semibold text-mint-ink">
      {t('appShell.directTag')}
    </span>
  );
};
