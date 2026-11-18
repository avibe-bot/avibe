import { useRouteSurfaceActive } from '@/lib/routeSurfaceActivity';
import { useCallback, useEffect, useRef, useState } from 'react';
import { connectHealthRequest, getHealthRequest, healthRequestView, type HealthRequest, type HealthViewState } from '@/lib/healthRequests';

export function useHealthRequest(id: string, initial?: HealthRequest) {
  const active = useRouteSurfaceActive();
  const [request, setRequest] = useState(initial);
  const [loadError, setLoadError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [connection, setConnection] = useState<HealthViewState | null>(null);
  const mounted = useRef(true);
  const inFlight = useRef(false);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const next = await getHealthRequest(id, signal);
      if (!signal?.aborted && mounted.current) { setRequest(next); setLoadError(false); }
      return next;
    } catch {
      if (!signal?.aborted && mounted.current) setLoadError(true);
    }
  }, [id]);
  useEffect(() => {
    mounted.current = true;
    if (!active) return () => { mounted.current = false; };
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      const next = await refresh(abort.signal);
      if (!abort.signal.aborted && (!next || ['queued', 'claimed'].includes(next.state))) timer = setTimeout(poll, 3000);
    };
    void poll();
    return () => { mounted.current = false; abort.abort(); clearTimeout(timer); };
  }, [refresh, active]);
  const authorize = async () => {
    if (!request || inFlight.current || loadError) return;
    inFlight.current = true;
    setBusy(true);
    try {
      const result = await connectHealthRequest(request);
      if (mounted.current) setConnection(result.state);
    } catch {
      if (mounted.current) setConnection('failed');
    } finally {
      inFlight.current = false;
      if (mounted.current) { setBusy(false); void refresh(); }
    }
  };
  const serverView = request ? healthRequestView(request) : null;
  // A bridge result never changes the request. Only queued/claimed may show the connection receipt.
  const state = request && (request.state === 'queued' || request.state === 'claimed') && connection
    ? connection : serverView;
  return { request, state, busy, loadError, authorize };
}
