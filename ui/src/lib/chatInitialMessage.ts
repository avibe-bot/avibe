export type InitialMessageHandoff = {
  message: string;
  sessionId: string;
};

export const pendingInitialMessageHandoff = ({
  handledSessionId,
  loadedSessionId,
  loading,
  locationState,
  routeSurfaceActive,
  sessionId,
}: {
  handledSessionId: string | null;
  loadedSessionId: string | undefined;
  loading: boolean;
  locationState: unknown;
  routeSurfaceActive: boolean;
  sessionId: string | undefined;
}): InitialMessageHandoff | null => {
  if (!routeSurfaceActive || loading || !sessionId || loadedSessionId !== sessionId) return null;
  if (handledSessionId === sessionId) return null;

  const message = (locationState as { initialMessage?: unknown } | null)?.initialMessage;
  return typeof message === 'string' && message.length > 0 ? { message, sessionId } : null;
};
