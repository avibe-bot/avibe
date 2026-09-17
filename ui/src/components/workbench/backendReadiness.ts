/**
 * What the home knows about the backend it would actually run on, as reported by
 * the backend itself. `null` means nothing has been observed yet — which is not
 * the same as "not ready", and must not be rounded up or down.
 */
export interface BackendReadiness {
  backend: string;
  ready: boolean;
}

export interface ReadyBannerInput {
  /** The setup wizard lands on `/` with `{ onboardingCompleted: true }` in router
   *  state. History state SURVIVES a reload, so the home consumes the key once
   *  and replaces the entry without it; what arrives here is that latch, held
   *  for the visit so a slow readiness answer can still find it. */
  onboardingCompleted: boolean;
  /** Corroboration from the backend. */
  readiness: BackendReadiness | null;
  /** The backend the home's current Agent route would actually run. */
  currentBackend: string | null;
  dismissed: boolean;
}

/**
 * The banner states a fact about the running system, so every part of it has to
 * be observed: the wizard reported it finished, the backend reports itself ready,
 * and the backend that reported is the one this home would use. A probe that is
 * pending, failed, or answers for a different backend renders nothing — an
 * unverified guess here is a false claim, not an optimistic one.
 */
export const shouldShowReadyBanner = (input: ReadyBannerInput): boolean => {
  if (!input.onboardingCompleted || input.dismissed) return false;
  if (!input.readiness || input.readiness.ready !== true) return false;
  if (!input.currentBackend) return false;
  return input.readiness.backend === input.currentBackend;
};

/**
 * The one remaining producer seam. #2011 owns `useApi().getBackendConnection()`
 * and `GET /api/backend/{backend}/connection`; until that lands there is nothing
 * corroborated to read, so this returns `null` and the home shows no banner.
 * Wiring it is this function body — probe `backend`, return `{ backend, ready }`
 * — and nothing else: the predicate, the copy and the presentation are done.
 * The hookup must require an ok response AND `ready` AND that the answer names
 * the backend it was asked about; a result arriving after the Agent route moved
 * on belongs to the backend it was requested for, and the predicate drops it.
 */
export const useBackendReadiness = (backend: string | null): BackendReadiness | null => {
  void backend;
  return null;
};
