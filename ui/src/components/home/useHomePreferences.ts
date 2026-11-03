import { useCallback, useEffect, useRef, useState } from 'react';

import { useApi } from '../../context/ApiContext';
import { DEFAULT_HOME_PREFERENCES, type HomePreferences } from '../../lib/homePreferences';
import { useRouteSurfaceActive } from '../../lib/routeSurfaceActivity';
import { useCoalescedWrite } from '../../lib/useCoalescedWrite';
import { useLatestRef } from '../../lib/useLatestRef';

export interface HomePreferencesState {
  preferences: HomePreferences;
  /** The record has been read (or the read failed, and the defaults stand). Until then nothing
   *  the record can hide is shown, so a card put off today does not flash up first. */
  loaded: boolean;
  /** Apply a change at once and save it behind the screen. */
  update: (changes: Partial<HomePreferences>) => void;
}

type Changes = Partial<HomePreferences>;

/** The one account record every surface writes (the home, Settings · General). */
const WRITE_KEY = 'account';

/** What `setup_not_needed` becomes: it only ever grows, on screen as on the server. */
const withNotNeeded = (current: HomePreferences['setup_not_needed'], added: HomePreferences['setup_not_needed'] = []) =>
  [...new Set([...current, ...added])];

/** Changes made while a save is in flight fold into the next one: the newer value wins, and items
 *  marked not needed add up. */
const mergeChanges = (prev: Changes, next: Changes): Changes => ({
  ...prev,
  ...next,
  ...(prev.setup_not_needed || next.setup_not_needed
    ? { setup_not_needed: withNotNeeded(prev.setup_not_needed ?? [], next.setup_not_needed) }
    : {}),
});

/**
 * The account's home preferences (`/api/home/preferences`). Read when shown, again when the
 * surface is uncovered or the window regains focus (another device may have changed them).
 *
 * A change shows at once and is saved through one writer for the account (`useCoalescedWrite`):
 * one request in flight, the changes made meanwhile folded into the next, so the newest choice is
 * also the last one the server commits. Each field stands on its own, so a refused save does not
 * drop the changes waiting behind it. Once a burst settles the record is read back.
 */
export function useHomePreferences(): HomePreferencesState {
  const api = useApi();
  const surfaceActive = useRouteSurfaceActive();
  const [preferences, setPreferences] = useState<HomePreferences>(DEFAULT_HOME_PREFERENCES);
  const [loaded, setLoaded] = useState(false);
  // Every change fences the reads started before it, so a slow read cannot put an old record back.
  const generationRef = useRef(0);

  const read = useCallback(async () => {
    const generation = ++generationRef.current;
    try {
      const next = await api.getHomePreferences();
      if (generation === generationRef.current) setPreferences(next);
    } catch {
      // Unreadable: the defaults (or what is on screen) stand.
    }
    if (generation === generationRef.current) setLoaded(true);
  }, [api]);

  const send = useCallback(async (changes: Changes) => {
    try {
      await api.updateHomePreferences(changes);
      return true;
    } catch {
      return false;
    }
  }, [api]);
  const { write, isSaving } = useCoalescedWrite<Changes>('home-preferences', send, {
    merge: mergeChanges,
    standsAlone: () => true,
    onSettled: useCallback(() => read(), [read]),
  });
  // Reads wait for the writer: the burst's own settle reads the record back.
  const savingRef = useLatestRef(isSaving(WRITE_KEY));

  useEffect(() => {
    if (!surfaceActive) return;
    queueMicrotask(() => {
      if (!savingRef.current) void read();
    });
    const onFocus = () => {
      if (!savingRef.current) void read();
    };
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [read, savingRef, surfaceActive]);

  const update = useCallback((changes: Changes) => {
    generationRef.current += 1;
    setPreferences((current) => ({
      ...current,
      ...changes,
      ...(changes.setup_not_needed ? { setup_not_needed: withNotNeeded(current.setup_not_needed, changes.setup_not_needed) } : {}),
    }));
    write(WRITE_KEY, changes);
  }, [write]);

  return { preferences, loaded, update };
}
