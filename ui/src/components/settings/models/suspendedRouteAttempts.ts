import type { SuspendedRouteAttempt } from "./RouteChainDialog";
import type { AgentBackend } from "./types";

export type SuspendedRouteAttempts = ReadonlyMap<
  AgentBackend,
  SuspendedRouteAttempt
>;

export const emptySuspendedRouteAttempts = (): SuspendedRouteAttempts =>
  new Map();

export const holdSuspendedRouteAttempt = (
  attempts: SuspendedRouteAttempts,
  attempt: SuspendedRouteAttempt,
): SuspendedRouteAttempts => {
  const next = new Map(attempts);
  next.set(attempt.backend, attempt);
  return next;
};

export const releaseSuspendedRouteAttempt = (
  attempts: SuspendedRouteAttempts,
  backend: AgentBackend,
): SuspendedRouteAttempts => {
  if (!attempts.has(backend)) return attempts;
  const next = new Map(attempts);
  next.delete(backend);
  return next;
};
