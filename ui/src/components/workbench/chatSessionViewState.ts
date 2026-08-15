export type ChatSessionViewState = 'loading' | 'ready' | 'failed';

type ChatSessionViewStateInput = {
  routeSessionId: string;
  loadedSessionId: string | null;
  hydratedTranscriptSessionId: string | null;
  failedBootstrapSessionId: string | null;
};

export function chatSessionViewState({
  routeSessionId,
  loadedSessionId,
  hydratedTranscriptSessionId,
  failedBootstrapSessionId,
}: ChatSessionViewStateInput): ChatSessionViewState {
  if (
    loadedSessionId === routeSessionId
    && hydratedTranscriptSessionId === routeSessionId
  ) return 'ready';

  if (failedBootstrapSessionId === routeSessionId) return 'failed';

  // The lightweight Session-row recovery can beat the transcript bootstrap.
  // Neither that row nor an empty messages array proves the route is ready: only
  // the route-scoped hydration marker can distinguish a real empty transcript
  // from the reset state that exists while bootstrap is still in flight.
  return 'loading';
}
