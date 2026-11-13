import { useState } from 'react';
import type { DesktopUpdatePhase } from '../../lib/desktopShell';

/**
 * The desktop app's `error` phase does not say whether a check or an install failed. A surface
 * that asked for an install, and had it accepted, knows: an `error` after that is the install's.
 * A rejected request is never accepted, so it is never taken for a failed install, and the next
 * check or settled result ends that install story.
 */
export function useAcceptedInstall(phase: DesktopUpdatePhase | undefined) {
  const [accepted, setAccepted] = useState(false);
  const [seen, setSeen] = useState(phase);
  if (phase !== seen) {
    setSeen(phase);
    if (phase === 'checking' || phase === 'current' || phase === 'available' || phase === 'idle') {
      setAccepted(false);
    }
  }
  return {
    failed: accepted && phase === 'error',
    accept: () => setAccepted(true),
    reset: () => setAccepted(false),
  };
}
