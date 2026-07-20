export type WorkbenchEventConnectionState = 'connected' | 'reconnecting' | 'disconnected';

// EventSource keeps readyState=CONNECTING (0) while its built-in retry loop is
// active. CLOSED (2), or any non-standard terminal value, means it has stopped
// retrying and needs an external recovery signal such as visibility/online.
export function eventSourceErrorConnectionState(readyState: number): WorkbenchEventConnectionState {
  return readyState === 0 ? 'reconnecting' : 'disconnected';
}
