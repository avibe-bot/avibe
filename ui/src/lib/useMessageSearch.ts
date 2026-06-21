import { useEffect, useRef, useState } from 'react';

import { useApi, type MessageSearchResult } from '../context/ApiContext';

export type UseMessageSearchOptions = {
  // Minimum trimmed query length before a request fires. Below this the hook
  // stays idle (results null, not loading) so an empty/near-empty palette
  // doesn't hammer the endpoint. Defaults to 1.
  minLength?: number;
  // Debounce window in ms — the query must stop changing this long before a
  // request is sent. Defaults to 200.
  debounceMs?: number;
};

export type UseMessageSearchState = {
  results: MessageSearchResult | null;
  loading: boolean;
  error: string | null;
};

// Debounced message-content search shared by the desktop command palette (P3)
// and the mobile search page (P4). Trims the query, gates on ``minLength``,
// debounces, and tags every request with a monotonic sequence so a slow
// earlier response can never clobber a newer one (out-of-order safety).
export function useMessageSearch(
  query: string,
  opts?: UseMessageSearchOptions,
): UseMessageSearchState {
  const { searchMessages } = useApi();
  const minLength = opts?.minLength ?? 1;
  const debounceMs = opts?.debounceMs ?? 200;

  const [results, setResults] = useState<MessageSearchResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Bumped on every fired request; a resolved response only commits if it is
  // still the latest. Survives re-renders so stale in-flight calls are ignored.
  const seqRef = useRef(0);

  useEffect(() => {
    const trimmed = query.trim();

    // Below the threshold: clear to the idle state and skip the request. Bump
    // the seq so any in-flight response from a longer prior query is dropped.
    if (trimmed.length < minLength) {
      seqRef.current += 1;
      setResults(null);
      setLoading(false);
      setError(null);
      return;
    }

    // Drop the PREVIOUS query's hits immediately (before the debounce/fetch) so
    // the palette/page never render — and let the user select — stale rows under
    // a changed query (Enter/tap could otherwise open a non-matching message).
    // The loading spinner covers the gap until the new results resolve; the
    // seq guard below still protects against out-of-order responses.
    setResults(null);
    setLoading(true);
    setError(null);

    const timer = window.setTimeout(() => {
      const seq = (seqRef.current += 1);
      searchMessages(trimmed)
        .then((res) => {
          if (seq !== seqRef.current) return;
          setResults(res);
          setLoading(false);
        })
        .catch((err: unknown) => {
          if (seq !== seqRef.current) return;
          setResults(null);
          setError(err instanceof Error ? err.message : String(err));
          setLoading(false);
        });
    }, debounceMs);

    // A new query (or unmount) cancels the pending debounce; bumping the seq
    // also invalidates any request that already left the gate.
    return () => {
      window.clearTimeout(timer);
      seqRef.current += 1;
    };
  }, [query, minLength, debounceMs, searchMessages]);

  return { results, loading, error };
}
