import { useEffect, useSyncExternalStore } from 'react';

/**
 * Draft, in-flight send, and per-session uncertainty for the pet window.
 * Held once per document outside React so an AuthGuard recheck — which
 * unmounts the page while it revalidates — cannot drop a send lock or the
 * text that belongs to it.
 */
let draft = '';
let draftSession: string | null | undefined;
const uncertainSessions = new Set<string>();
let sending = false;
let version = 0;
const listeners = new Set<() => void>();

const notify = () => {
  version += 1;
  listeners.forEach((listener) => listener());
};

const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
};

export const petComposer = {
  isUncertain: (sessionId: string | null): boolean => (
    Boolean(sessionId && uncertainSessions.has(sessionId))
  ),
  isSending: (): boolean => sending,
  setUncertain(sessionId: string, value: boolean) {
    const had = uncertainSessions.has(sessionId);
    if (value === had) return;
    if (value) uncertainSessions.add(sessionId);
    else uncertainSessions.delete(sessionId);
    notify();
  },
  setSending(value: boolean) {
    if (sending === value) return;
    sending = value;
    notify();
  },
  setDraft(value: string, sessionId: string | null) {
    const sessionChanged = draftSession !== sessionId;
    if (!sessionChanged && draft === value) return;
    draftSession = sessionId;
    draft = value;
    notify();
  },
  bind(sessionId: string | null) {
    if (draftSession === sessionId) return;
    draftSession = sessionId;
    draft = '';
    notify();
  },
  clearDraftIf(value: string) {
    if (draft !== value) return;
    draft = '';
    notify();
  },
};

export function usePetComposer(binding: string | null): {
  draft: string;
  uncertain: boolean;
  sending: boolean;
  setDraft: (value: string) => void;
  setUncertain: (sessionId: string, value: boolean) => void;
} {
  useSyncExternalStore(subscribe, () => version);
  useEffect(() => {
    petComposer.bind(binding);
  }, [binding]);
  return {
    // A draft belongs to the session it was typed for. Until bind lands,
    // hide text that still belongs to the previous binding.
    draft: draftSession === binding ? draft : '',
    uncertain: petComposer.isUncertain(binding),
    sending,
    setDraft: (value: string) => petComposer.setDraft(value, binding),
    setUncertain: petComposer.setUncertain,
  };
}
