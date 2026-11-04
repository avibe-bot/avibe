export type ChatSessionViewState = 'loading' | 'ready' | 'failed';

type ChatSessionViewStateInput = {
  routeSessionId: string;
  loadedSessionId: string | null;
  hydratedTranscriptSessionId: string | null;
  failedBootstrapSessionId: string | null;
  /** The route whose last known snapshot is painted while its bootstrap is out. */
  snapshotSessionId?: string | null;
};

export function chatSessionViewState({
  routeSessionId,
  loadedSessionId,
  hydratedTranscriptSessionId,
  failedBootstrapSessionId,
  snapshotSessionId = null,
}: ChatSessionViewStateInput): ChatSessionViewState {
  if (
    loadedSessionId === routeSessionId
    && hydratedTranscriptSessionId === routeSessionId
  ) return 'ready';

  if (failedBootstrapSessionId === routeSessionId) return 'failed';

  // A painted snapshot renders the chat until the bootstrap answers. It never
  // stands in for a failed bootstrap, and it is not hydration: the live loading
  // state stays separate (see useChatSnapshot).
  if (snapshotSessionId === routeSessionId) return 'ready';

  // The lightweight Session-row recovery can beat the transcript bootstrap.
  // Neither that row nor an empty messages array proves the route is ready: only
  // the route-scoped hydration marker can distinguish a real empty transcript
  // from the reset state that exists while bootstrap is still in flight.
  return 'loading';
}
