// Starting and observing the model runtime. Both helpers read supervisor health
// back from the server rather than trusting the local optimistic guess, so the
// persistent Models page never keeps presenting lazy-start idleness after a
// failed start. Pure of React so the contract is unit-testable
// (see RuntimeNotStartedAction.test.tsx).
import type { ModelsApi } from './modelsApi';
import type { AgentSupply, RuntimeDependency } from './types';

const runtimeIsInstalled = (runtime: RuntimeDependency): boolean =>
  runtime.status.health !== 'not_installed' && runtime.status.health !== 'installing';

export const runtimeIsRunning = (runtime: RuntimeDependency): boolean =>
  runtime.status.health === 'ok' || runtime.status.health === 'degraded';

export const agentHasLiveChainProjection = (
  runtime: RuntimeDependency | null,
  agent: AgentSupply,
): boolean => runtime !== null && runtimeIsRunning(runtime) && agent.mode === 'hub';

export const runtimeHasInstallAsset = (runtime: RuntimeDependency): boolean => {
  const host = 'host_platform' in runtime && typeof runtime.host_platform === 'string'
    ? runtime.host_platform
    : null;
  return runtime.manifest.assets.some((asset) => host == null || asset.platform === host);
};

const waitForPoll = (intervalMs: number): Promise<void> => new Promise((resolve) => {
  globalThis.setTimeout(resolve, intervalMs);
});

export async function installRuntimeUntilSettled(
  api: Pick<ModelsApi, 'installRuntime' | 'getRuntimeStatus'>,
  onRuntime: (runtime: RuntimeDependency) => void = () => {},
  intervalMs = 2_000,
  initialRuntime?: RuntimeDependency,
): Promise<{ runtime: RuntimeDependency | null; failed: boolean }> {
  let runtime: RuntimeDependency | null = initialRuntime ?? null;
  if (!runtime) {
    try {
      runtime = await api.installRuntime();
    } catch {
      // The install request is not safe to repeat until supervisor state proves
      // it did not start. Reconcile the authoritative state first.
      runtime = await api.getRuntimeStatus().catch(() => null);
    }
  }

  if (!runtime) return { runtime: null, failed: true };
  onRuntime(runtime);
  while (runtime.status.health === 'installing') {
    await waitForPoll(intervalMs);
    runtime = await api.getRuntimeStatus().catch(() => null);
    if (!runtime) return { runtime: null, failed: true };
    onRuntime(runtime);
  }
  return { runtime, failed: !runtimeIsInstalled(runtime) };
}

export async function startRuntimeWithStatusRefresh(
  api: Pick<ModelsApi, 'startRuntime' | 'getRuntimeStatus'>,
): Promise<{ runtime: RuntimeDependency | null; failed: boolean }> {
  try {
    const runtime = await api.startRuntime();
    return {
      runtime,
      failed: runtime.status.health !== 'ok' && runtime.status.health !== 'degraded',
    };
  } catch {
    // A failed start changes supervisor health. Read that authoritative state
    // back so the persistent page does not keep presenting lazy-start idleness.
    const runtime = await api.getRuntimeStatus().catch(() => null);
    return {
      runtime,
      failed: runtime?.status.health !== 'ok' && runtime?.status.health !== 'degraded',
    };
  }
}

export function pollRuntimeStatus(
  api: Pick<ModelsApi, 'getRuntimeStatus'>,
  onRuntime: (runtime: RuntimeDependency) => void,
  intervalMs = 5_000,
): () => void {
  let active = true;
  let timeout: ReturnType<typeof globalThis.setTimeout> | undefined;
  const schedule = () => {
    timeout = globalThis.setTimeout(() => void refresh(), intervalMs);
  };
  const refresh = async () => {
    try {
      const runtime = await api.getRuntimeStatus();
      if (active) onRuntime(runtime);
    } catch {
      // Keep the last authoritative snapshot and try again on the next tick.
    } finally {
      if (active) schedule();
    }
  };
  schedule();
  return () => {
    active = false;
    if (timeout !== undefined) globalThis.clearTimeout(timeout);
  };
}
