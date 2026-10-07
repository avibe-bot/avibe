// The assistants, as the shape the introduction's cards shrink into.
//
// This row is the handoff's landing target (C3), which is why it carries no state of
// its own: an enabled/disabled mark here would be a second, staler answer to a
// question screen 3 owns, and the handoff would have to animate it appearing. The
// row says who the routing is for and nothing else — including that the built-in one
// is part of the platform rather than something to install.
import type { FC } from 'react';
import { useTranslation } from 'react-i18next';
import { getBackendUiMeta } from '@/lib/agentBackends';

import { SETUP_LINEUP } from '../collaborationTimeline';
import { BackendIcon } from '../../visual';

export const DestinationRow: FC = () => {
  const { t } = useTranslation();
  return (
    <div className="setup-destinations">
      {SETUP_LINEUP.map((backend) => (
        <div key={backend} className="setup-destination" data-backend={backend}>
          <span className="setup-destination-logo">
            <BackendIcon backend={backend} size={28} variant="brand" aria-hidden="true" />
          </span>
          <span className="setup-destination-name">{getBackendUiMeta(backend).label}</span>
          {getBackendUiMeta(backend).builtin && (
            <span className="setup-destination-tag">{t('settings.backends.builtinBadge')}</span>
          )}
        </div>
      ))}
    </div>
  );
};
