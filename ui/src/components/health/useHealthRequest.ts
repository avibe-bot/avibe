import { useRouteSurfaceActive } from '@/lib/routeSurfaceActivity';
import { useEffect, useRef, useState } from 'react';
import { connectHealthRequest, getHealthRequest, healthRequestView, type HealthRequest, type HealthViewState } from '@/lib/healthRequests';

type LiveState = {
  id: string;
  request?: HealthRequest;
  known: boolean;
  loadError: boolean;
  busy: boolean;
  connection: HealthViewState | null;
};
const unknownState = (id: string): LiveState => ({ id, known: false, loadError: false, busy: false, connection: null });
const terminal = (request?: HealthRequest) => request && !['queued', 'claimed'].includes(request.state);

export function useHealthRequest(id: string, initial?: HealthRequest) {
  const active = useRouteSurfaceActive();
  const [live, setLive] = useState<LiveState>(() => unknownState(id));
  const owner = useRef<{ id: string; authorize: () => Promise<void> } | null>(null);

  useEffect(() => {
    // One lifetime owns both reads and the pairing attempt. A route change retires all of them.
    const lifetime = new AbortController();
    let snapshot = unknownState(id);
    let read: AbortController | undefined;
    let sequence = 0;
    let failures = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const publish = () => { if (!lifetime.signal.aborted) setLive({ ...snapshot }); };
    publish();

    const refresh = async () => {
      if (lifetime.signal.aborted || terminal(snapshot.request)) return;
      clearTimeout(timer);
      read?.abort();
      const attempt = new AbortController();
      read = attempt;
      const current = ++sequence;
      const ownsRead = () => !lifetime.signal.aborted && !attempt.signal.aborted && current === sequence;
      try {
        const next = await getHealthRequest(id, attempt.signal);
        if (!ownsRead()) return;
        snapshot = { ...snapshot, request: next, known: true, loadError: false };
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
        if (ownsRead() && !terminal(snapshot.request) && failures < 3) {
          timer = setTimeout(() => { void refresh(); }, 3000);
        }
      }
    };

    const authorize = async () => {
      const request = snapshot.request;
      if (lifetime.signal.aborted || snapshot.busy || !snapshot.known || snapshot.loadError
        || !request || healthRequestView(request) !== 'authorize') return;
      snapshot = { ...snapshot, busy: true };
      publish();
      try {
        const result = await connectHealthRequest(request, lifetime.signal);
        if (!lifetime.signal.aborted) snapshot = { ...snapshot, connection: result.state };
      } catch {
        if (!lifetime.signal.aborted) snapshot = { ...snapshot, connection: 'failed' };
      } finally {
        if (!lifetime.signal.aborted) {
          snapshot = { ...snapshot, busy: false };
          publish();
          void refresh();
        }
      }
    };

    owner.current = active ? { id, authorize } : null;
    if (active) void refresh();
    return () => {
      owner.current = null;
      lifetime.abort();
      read?.abort();
      clearTimeout(timer);
    };
  }, [id, active]);

  // Creation metadata may draw a card, but only this route's successful live read opens its action.
  const current = live.id === id ? live : unknownState(id);
  const request = current.request ?? initial;
  const serverView = request ? healthRequestView(request) : null;
  const state = request && !terminal(request) && current.connection ? current.connection : serverView;
  const authorize = async () => {
    if (active && owner.current?.id === id) await owner.current.authorize();
  };
  return { request, state, busy: current.busy, loadError: current.loadError,
    canAuthorize: active && current.known && !current.loadError, authorize };
}
