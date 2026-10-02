// The three assistants, as the shape the introduction's cards shrink into.
//
// This row is the handoff's landing target (C3), which is why it carries no state of
// its own: an enabled/disabled mark here would be a second, staler answer to a
// question screen 3 owns, and the handoff would have to animate it appearing. The
// row says who the routing is for and nothing else.
import type { FC } from 'react';
import { getBackendUiMeta } from '@/lib/agentBackends';

import { ASSISTANT_ORDER } from '../collaborationTimeline';
import { BackendIcon } from '../../visual';

export const DestinationRow: FC = () => {
  return (
    <div className="setup-destinations">
      {ASSISTANT_ORDER.map((backend) => (
        <div key={backend} className="setup-destination" data-backend={backend}>
          <span className="setup-destination-logo">
            <BackendIcon backend={backend} size={28} variant="brand" aria-hidden="true" />
          </span>
          <span className="setup-destination-name">{getBackendUiMeta(backend).label}</span>
        </div>
      ))}
    </div>
  );
};
