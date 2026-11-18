import { useRouteSurfaceActive } from '@/lib/routeSurfaceActivity';
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { connectHealthRequest, getHealthConnection, getHealthRequest, healthRequestView, observeHealthTerminal, subscribeHealthConnection, type HealthRequest } from '@/lib/healthRequests';

type LiveState = {
  id: string;
  request?: HealthRequest;
  known: boolean;
  loadError: boolean;
};
const unknownState = (id: string): LiveState => ({ id, known: false, loadError: false });
const terminal = (request?: HealthRequest) => request && !['queued', 'claimed'].includes(request.state);

export function useHealthRequest(id: string, initial?: HealthRequest) {
  const active = useRouteSurfaceActive();
  const [live, setLive] = useState<LiveState>(() => unknownState(id));
  const owner = useRef<{ id: string; authorize: () => Promise<void> } | null>(null);
  const connection = useSyncExternalStore(subscribeHealthConnection, getHealthConnection, getHealthConnection);

  useEffect(() => {
    // Visibility owns reads and pre-dispatch pairing only. Dispatched Native has its own owner.
    const lifetime = new AbortController();
    let snapshot = unknownState(id);
    const sharedTerminal = () => {
      const entry = getHealthConnection().entries.get(id);
      return entry?.kind === 'terminal' ? entry.request : undefined;
    };
    snapshot.request = sharedTerminal();
    let read: AbortController | undefined;
    let sequence = 0;
    let failures = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const publish = () => { if (!lifetime.signal.aborted) setLive({ ...snapshot }); };
    publish();

    const refresh = async () => {
      if (lifetime.signal.aborted || sharedTerminal() || terminal(snapshot.request)) return;
      clearTimeout(timer);
      read?.abort();
      const attempt = new AbortController();
      read = attempt;
      const current = ++sequence;
      const ownsRead = () => !lifetime.signal.aborted && !attempt.signal.aborted && current === sequence;
      try {
        const next = await getHealthRequest(id, attempt.signal);
        if (!ownsRead()) return;
        if (terminal(next)) observeHealthTerminal(next);
        snapshot = { ...snapshot, request: sharedTerminal() ?? next, known: true, loadError: false };
        failures = 0;
        publish();
      } catch {
        if (!ownsRead()) return;
        snapshot = { ...snapshot, known: false, loadError: true };
        failures++;
        publish();
      } finally {
        // Both poll and post-authorization reads schedule here; a superseded read cannot re-arm.
        // After three failed reads, reopening the page starts a fresh bounded attempt window.
        if (ownsRead() && !sharedTerminal() && !terminal(snapshot.request) && failures < 3) {
          timer = setTimeout(() => { void refresh(); }, 3000);
        }
      }
    };

    const authorize = async () => {
      const request = snapshot.request;
      if (lifetime.signal.aborted || !snapshot.known || snapshot.loadError
        || !request || healthRequestView(request) !== 'authorize') return;
      await connectHealthRequest(request, lifetime.signal);
    };

    owner.current = active ? { id, authorize } : null;
    // Reopened consumers read immediately; only a new matching completion supersedes that read.
    let previousEntry = getHealthConnection().entries.get(id);
    const unsubscribe = subscribeHealthConnection(() => {
      const next = getHealthConnection().entries.get(id);
      if (next === previousEntry) return;
      previousEntry = next;
      if (next?.kind === 'terminal') {
        // A peer's confirmed terminal supersedes this consumer's queued/error/in-flight read.
        read?.abort();
        clearTimeout(timer);
        snapshot = { ...snapshot, request: next.request };
        publish();
      } else if (active && next?.kind === 'receipt') void refresh();
    });
    if (active) void refresh();
    return () => {
      owner.current = null;
      unsubscribe();
      lifetime.abort();
      read?.abort();
      clearTimeout(timer);
    };
  }, [id, active]);

  // Creation metadata may draw a card, but only this route's successful live read opens its action.
  const current = live.id === id ? live : unknownState(id);
  const entry = connection.entries.get(id);
  const request = entry?.kind === 'terminal' ? entry.request : current.request ?? initial;
  const serverView = request ? healthRequestView(request) : null;
  const receipt = entry?.kind === 'receipt' ? entry.result : null;
  const pending = connection.active;
  const state = request && !terminal(request) && receipt ? receipt : serverView;
  const authorize = async () => {
    if (active && owner.current?.id === id) await owner.current.authorize();
  };
  return { request, state, busy: pending?.requestId === id, loadError: current.loadError,
    canAuthorize: active && current.known && !current.loadError && !pending && !entry, authorize };
}
