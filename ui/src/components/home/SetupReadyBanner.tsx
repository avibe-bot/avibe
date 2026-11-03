import { useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { ReadyBanner } from '../workbench/ReadyBanner';
import { shouldShowReadyBanner, useBackendReadiness, useSetupHandoff } from '../workbench/backendReadiness';

/**
 * The announcement the setup wizard hands the home: once the backend the home would run on
 * confirms it is ready, a banner says so, until it is dismissed. In the home's group it is the
 * first block while it shows; it re-centers the group like any block that comes or goes.
 *
 * The wizard hands `{ onboardingCompleted: true }` to this route. That is an event, not a property
 * of the location: history state survives a reload, so leaving the key in place would re-announce
 * the same setup every time the user came back here. This takes it out of the entry, keeping the
 * rest of the state so the overlay origin and anything else travelling with it stays intact, and
 * holds the event for the page (see `useSetupHandoff`: the home is remounted mid-handoff, and a
 * readiness answer can still arrive after that). The backend itself must still confirm it is
 * ready, so holding it alone shows nothing: it only makes the question worth asking.
 */
export function SetupReadyBanner({ backend }: { backend: string | null }) {
  const location = useLocation();
  const navigate = useNavigate();
  const wizardState = location.state as Record<string, unknown> | null;
  const wizardJustFinished = Boolean(wizardState?.onboardingCompleted);
  const { completed, dismissed, dismiss } = useSetupHandoff(wizardJustFinished);
  useEffect(() => {
    if (!wizardState?.onboardingCompleted) return;
    const { onboardingCompleted: _consumed, ...rest } = wizardState;
    navigate(`${location.pathname}${location.search}${location.hash}`, {
      replace: true,
      state: Object.keys(rest).length > 0 ? rest : null,
    });
  }, [location.hash, location.pathname, location.search, wizardState, navigate]);
  // Asked only while there is a completion left to announce: an ordinary visit,
  // and every visit after the user has dismissed the banner, reads nothing.
  const readiness = useBackendReadiness(backend, completed && !dismissed);
  const show = shouldShowReadyBanner({ onboardingCompleted: completed, readiness, currentBackend: backend, dismissed });
  if (!show || !backend) return null;
  return <ReadyBanner backend={backend} onDismiss={dismiss} />;
}
