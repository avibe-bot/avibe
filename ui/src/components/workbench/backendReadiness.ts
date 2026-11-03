import { useCallback, useEffect, useState, useSyncExternalStore } from 'react';
import { useApi, type BackendConnectionState } from '../../context/ApiContext';
import { isBackend } from '../../lib/backendAccent';
import { useLatestRef } from '../../lib/useLatestRef';

/**
 * What the home knows about the backend it would actually run on, as reported by
 * the backend itself. `null` means nothing has been observed yet — which is not
 * the same as "not ready", and must not be rounded up or down.
 *
 * This is a view of the connection state the backend settings own, narrowed to
 * the two fields a banner is allowed to speak about. It is derived from that
 * type rather than restated, so the home cannot drift into describing a shape
 * the endpoint does not return.
 */
export type BackendReadiness = Pick<BackendConnectionState, 'backend' | 'ready'>;

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

/** What this page still owes the user about the setup the wizard just finished:
 *  nothing, an announcement it has not made yet, or one already waved off. */
type Handoff = 'none' | 'owed' | 'settled';

/**
 * Held for the page rather than for the component, which is not a detail.
 *
 * The completion arrives as router state on a navigation that crosses the setup
 * boundary — and crossing it is exactly what makes the route guard re-validate,
 * so the home is unmounted and remounted underneath it while that runs, after
 * the history entry carrying the event has already been consumed. A latch in
 * component state dies with that remount, and the completion could then never be
 * announced at all, however quickly the backend answered.
 *
 * So the lifetime is not the home's own. It has to outlive a remount the home
 * does not survive, and it has to end when the user actually leaves the visit it
 * was made for — which is a fact about the route, not about a component, and is
 * ended by `useSetupHandoffDeparture` below. A reload ends it as well, being a
 * different page reading an entry that no longer claims a setup just finished.
 */
let handoff: Handoff = 'none';
const watchers = new Set<() => void>();
const subscribe = (watcher: () => void) => {
  watchers.add(watcher);
  return () => { watchers.delete(watcher); };
};
const record = (next: Handoff) => {
  if (next === handoff) return;
  handoff = next;
  watchers.forEach((watcher) => watcher());
};

/** A new page, which is the only thing that forgets a handoff on its own. Tests
 *  share one module across many pages and start each one from here. */
export const forgetSetupHandoff = () => record('none');

export interface SetupHandoff {
  /** The wizard reported a finished setup to this page. */
  completed: boolean;
  /** ...and the user has already waved the announcement off. */
  dismissed: boolean;
  dismiss: () => void;
}

/**
 * Holds the wizard's completion for the page, and remembers a dismissal the same
 * way: a remount mid-visit loses neither, and the page ends both.
 *
 * Read as what it is — state outside React that a component subscribes to — so a
 * home mounting after the event is handed the same answer as one that was there
 * to receive it.
 */
export const useSetupHandoff = (arrived: boolean): SetupHandoff => {
  const held = useSyncExternalStore(subscribe, () => handoff);
  // The completion can arrive at an already mounted home too, and it is only
  // ever taken once: a dismissal is the user's answer to it, not a state to
  // overwrite the next time this runs.
  useEffect(() => {
    if (arrived && handoff === 'none') record('owed');
  }, [arrived]);
  const dismiss = useCallback(() => record('settled'), []);
  return { completed: held !== 'none', dismissed: held === 'settled', dismiss };
};

/** The route the handoff is about: the home the wizard hands off to. */
const HOME_PATH = '/';

/**
 * Ends the handoff when the user actually leaves the visit it was made for.
 *
 * The home is unmounted for two unrelated reasons — the route guard
 * re-validating at the setup boundary, and the user going somewhere else — and
 * only the route tells them apart, which is the whole reason this reads the
 * route being shown instead of the home's own lifecycle. Ending it on unmount
 * would end it during the guard's remount too, which is the bug the page scope
 * exists to fix.
 *
 * `shownPath` is what the route surface is actually RENDERING, which is why
 * Settings opened over the home is not a departure: on either form factor the
 * home is still there behind that surface, still holding a banner the user has
 * not answered. It is also not the shell's own chrome location, which follows
 * the foreground URL below md and would make a phone forget a handoff its home
 * is still holding.
 */
export const useSetupHandoffDeparture = (shownPath: string) => {
  useEffect(() => {
    if (shownPath !== HOME_PATH) record('none');
  }, [shownPath]);
};

/**
 * Only a backend the connection endpoint is defined for can be asked about. An
 * Agent route may name anything — an older route, a backend this build does not
 * know — and asking about one of those would produce an error toast over a home
 * the user is already using, to learn nothing. Unknown names are simply not a
 * question, so none is asked.
 *
 * The banner beside this one already names its backend through the same guard,
 * so the two agree on what a backend is by construction.
 */
const askable = (backend: string | null): BackendConnectionState['backend'] | null =>
  backend !== null && isBackend(backend) ? backend : null;

/**
 * Reads, once, whether the backend this home would run reports itself ready.
 *
 * It is a reader and nothing more: one GET per question asked, no polling, no
 * starting, no auth, no install, no write. Readiness is owned by the backend
 * settings and reported by their endpoint; the home only repeats what it was
 * told, and only about the backend it asked.
 *
 * `asked` is the question itself. A banner can only ever announce a completion
 * the home is still holding, so an ordinary visit — nothing to announce, or
 * something already announced and dismissed — asks nothing at all.
 *
 * Nothing observed here survives the question that produced it. When the Agent
 * route moves to another backend, or the home unmounts, the answer still on the
 * wire is about a question no longer being asked: it is dropped, and what was
 * already observed is cleared. That holds for A -> B -> A too, where the first
 * A's answer is stale despite naming the backend now in effect.
 */
export const useBackendReadiness = (backend: string | null, asked: boolean): BackendReadiness | null => {
  const api = useApi();
  // The api identity changes with the interface language, among other things.
  // Re-reading it here keeps a locale switch from re-probing the backend.
  const latest = useLatestRef(api);
  const name = asked ? askable(backend) : null;
  const [observed, setObserved] = useState<BackendReadiness | null>(null);
  const [answering, setAnswering] = useState(name);
  // Evidence belongs to the question that produced it, so a new question drops
  // it here rather than a frame later, and nothing stale is ever returned.
  const moved = answering !== name;
  if (moved) {
    setAnswering(name);
    setObserved(null);
  }

  useEffect(() => {
    if (!name) return;
    let current = true;
    void latest.current.getBackendConnection(name)
      .then((state) => {
        if (!current) return;
        // Every part has to hold: the read succeeded, the backend calls itself
        // ready, and the report is about the backend that was asked about.
        if (state?.ok !== true || state.ready !== true || state.backend !== name) return;
        setObserved({ backend: state.backend, ready: true });
      })
      // A refused, denied or unreachable read is not a report of "not ready" —
      // it is no report at all. The home stays exactly as silent, and usable.
      .catch(() => {});
    return () => { current = false; };
  }, [latest, name]);

  return moved ? null : observed;
};
