export type ChatSessionViewState = 'loading' | 'ready' | 'failed';

type ChatSessionViewStateInput = {
  routeSessionId: string;
  loadedSessionId: string | null;
  loading: boolean;
  error: string | null;
};

export function chatSessionViewState({
  routeSessionId,
  loadedSessionId,
  loading,
  error,
}: ChatSessionViewStateInput): ChatSessionViewState {
  if (loadedSessionId === routeSessionId) return 'ready';

  // A row from the previous route and an empty row awaiting a newer refresh are
  // both pending states. Absence becomes a failure only after a request records
  // an explicit error; it is never evidence that the session does not exist.
  if (loadedSessionId !== null || loading || error === null) return 'loading';

  return 'failed';
}
