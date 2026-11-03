import { useCallback, useEffect, useRef, useState } from 'react';

import { useApi } from '../../context/ApiContext';
import { DEFAULT_HOME_PREFERENCES, type HomePreferences } from '../../lib/homePreferences';
import { useRouteSurfaceActive } from '../../lib/routeSurfaceActivity';

export interface HomePreferencesState {
  preferences: HomePreferences;
  /** The record has been read (or the read failed, and the defaults stand). Until then nothing
   *  the record can hide is shown, so a card put off today does not flash up first. */
  loaded: boolean;
  /** Apply a change at once and save it; a refused save is read back over. */
  update: (changes: Partial<HomePreferences>) => void;
}

/**
 * The account's home preferences (`/api/home/preferences`). Read when shown, again when the
 * surface is uncovered or the window regains focus (another device may have changed them); a
 * change shows at once and saves only the fields it changes, which the server merges.
 */
export function useHomePreferences(): HomePreferencesState {
  const api = useApi();
  const surfaceActive = useRouteSurfaceActive();
  const [preferences, setPreferences] = useState<HomePreferences>(DEFAULT_HOME_PREFERENCES);
  const [loaded, setLoaded] = useState(false);
  // Every change fences the reads started before it, so a slow read cannot put an old record back.
  const generationRef = useRef(0);
  const pendingRef = useRef(0);

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

  useEffect(() => {
    if (!surfaceActive) return;
    queueMicrotask(() => void read());
    const onFocus = () => {
      if (pendingRef.current === 0) void read();
    };
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [read, surfaceActive]);

  const update = useCallback((changes: Partial<HomePreferences>) => {
    const generation = ++generationRef.current;
    pendingRef.current += 1;
    setPreferences((current) => ({
      ...current,
      ...changes,
      // The server only ever adds to it; so does the record on screen.
      ...(changes.setup_not_needed
        ? { setup_not_needed: [...new Set([...current.setup_not_needed, ...changes.setup_not_needed])] }
        : {}),
    }));
    void api.updateHomePreferences(changes)
      .then((saved) => {
        if (generation === generationRef.current) setPreferences(saved);
      })
      .catch(() => {
        if (generation === generationRef.current) void read();
      })
      .finally(() => {
        pendingRef.current -= 1;
      });
  }, [api, read]);

  return { preferences, loaded, update };
}
