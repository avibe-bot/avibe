import { useEffect, useMemo, useSyncExternalStore } from 'react';

import {
  canConfigureDesktopPet,
  desktopPetSettingsActions,
  listenDesktopPetSettings,
  type DesktopPetSettingsActions,
  type DesktopPetSettings,
} from './desktopShell';

type Snapshot = {
  supported: boolean;
  state: DesktopPetSettings | null;
  loading: boolean;
  error: string | null;
};

let snapshot: Snapshot = {
  supported: false,
  state: null,
  loading: false,
  error: null,
};
const listeners = new Set<() => void>();

const notify = () => listeners.forEach((listener) => listener());
const setSnapshot = (next: Snapshot) => {
  snapshot = next;
  notify();
};
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};

export function useDesktopPetSettings(): Snapshot & {
  actions: DesktopPetSettingsActions | null;
  refresh: () => Promise<void>;
  apply: (request: () => Promise<DesktopPetSettings>) => Promise<void>;
} {
  const supported = canConfigureDesktopPet();
  const actions = useMemo(() => desktopPetSettingsActions(), [supported]);
  const currentSnapshot = useSyncExternalStore(subscribe, () => snapshot, () => snapshot);

  useEffect(() => {
    if (!supported || !actions) {
      setSnapshot({ supported, state: null, loading: false, error: null });
      return undefined;
    }
    let alive = true;
    const applyState = (state: DesktopPetSettings) => {
      if (alive) setSnapshot({ supported: true, state, loading: false, error: null });
    };
    const stop = listenDesktopPetSettings(applyState);
    setSnapshot({ supported: true, state: snapshot.state, loading: true, error: null });
    void actions.getState().then(applyState).catch((error: unknown) => {
      if (!alive) return;
      setSnapshot({
        supported: true,
        state: null,
        loading: false,
        error: error instanceof Error ? error.message : String(error),
      });
    });
    return () => {
      alive = false;
      stop();
    };
  }, [actions, supported]);

  const refresh = async () => {
    const current = desktopPetSettingsActions();
    if (!supported || !current) return;
    const state = await current.getState();
    setSnapshot({ supported: true, state, loading: false, error: null });
  };

  const apply = async (request: () => Promise<DesktopPetSettings>) => {
    try {
      setSnapshot({ ...snapshot, supported: true, loading: true, error: null });
      const state = await request();
      setSnapshot({ supported: true, state, loading: false, error: null });
    } catch (error: unknown) {
      setSnapshot({
        ...snapshot,
        supported: true,
        loading: false,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  };

  return { ...currentSnapshot, actions, refresh, apply };
}
