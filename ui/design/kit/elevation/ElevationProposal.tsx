import type { ReactNode } from 'react';

import './elevation.css';

/** Draws its children under the approved elevation and edge system (ui/design: system/elevation/*).
 *  The stylesheet applies only while this marker is in the document, portals included, so a frame
 *  without it, and the canvas around the frames, render as shipped. */
export function ElevationProposal({ children }: { children: ReactNode }) {
  return (
    <div data-elevation-proposal className="contents">
      {children}
    </div>
  );
}
