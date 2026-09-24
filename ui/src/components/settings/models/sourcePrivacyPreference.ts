import { useSyncExternalStore } from 'react';

const STORAGE_KEY = 'avibe.model-hub.hide-private-details.v1';
const listeners = new Set<() => void>();
let hiddenInMemory: boolean | undefined;

const getHidden = () => {
  if (hiddenInMemory !== undefined) return hiddenInMemory;
  try {
    return localStorage.getItem(STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
};

const subscribe = (listener: () => void) => {
  listeners.add(listener);
  const onStorage = (event: StorageEvent) => {
    if (event.key === STORAGE_KEY || event.key === null) {
      hiddenInMemory = undefined;
      listener();
    }
  };
  window.addEventListener('storage', onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener('storage', onStorage);
  };
};

export const setSourceDetailsHidden = (hidden: boolean) => {
  try {
    localStorage.setItem(STORAGE_KEY, String(hidden));
    hiddenInMemory = undefined;
  } catch {
    // Browser storage can be disabled; visibility still works for this page.
    hiddenInMemory = hidden;
  }
  listeners.forEach((listener) => listener());
};

export const useSourceDetailsHidden = () => useSyncExternalStore(subscribe, getHidden, () => true);
