// Starting and observing the model runtime. Both helpers read supervisor health
// back from the server rather than trusting the local optimistic guess, so the
// persistent Models page never keeps presenting lazy-start idleness after a
// failed start. Pure of React so the contract is unit-testable
// (see RuntimeNotStartedAction.test.tsx).
import type { ModelsApi } from './modelsApi';
import { foldRegionRead, type RegionRead } from './regionRead';
import type { AgentSupply, RuntimeDependency } from './types';

const FRESH_RUNTIME: unique symbol = Symbol('model-hub-fresh-runtime');

export type FreshRuntimeProjection = {
  readonly [FRESH_RUNTIME]: RuntimeDependency;
};

export const freshRuntimeProjection = (
  read: RegionRead<RuntimeDependency>,
): FreshRuntimeProjection | null => foldRegionRead(read, {
  loading: () => null,
  ready: (runtime) => ({ [FRESH_RUNTIME]: runtime }),
  unread: () => null,
  degraded: () => null,
});

const runtimeIsInstalled = (runtime: RuntimeDependency): boolean =>
  runtime.status.health !== 'not_installed' && runtime.status.health !== 'installing';

export const runtimeIsRunning = (runtime: RuntimeDependency): boolean =>
  runtime.status.health === 'ok' || runtime.status.health === 'degraded';

export type InstallAndStartStep = 'install' | 'start' | 'complete';

export const INSTALL_AND_START_STEP = {
  not_installed: 'install',
  installing: 'install',
  not_started: 'start',
  down: 'start',
  ok: 'complete',
  degraded: 'complete',
} as const satisfies Record<RuntimeDependency['status']['health'], InstallAndStartStep>;

export const installAndStartStep = (runtime: RuntimeDependency): InstallAndStartStep =>
  INSTALL_AND_START_STEP[runtime.status.health];

export const agentHasLiveChainProjection = (
  runtime: FreshRuntimeProjection | null,
  agent: AgentSupply,
): boolean => runtime !== null && runtimeIsRunning(runtime[FRESH_RUNTIME]) && agent.mode === 'hub';

export const runtimeCanAttemptInstall = (runtime: RuntimeDependency): boolean => {
  const host = 'host_platform' in runtime && typeof runtime.host_platform === 'string'
    ? runtime.host_platform
    : null;
  return runtime.manifest.assets.length === 0
    || runtime.manifest.assets.some((asset) => host == null || asset.platform === host);
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
): Promise<{ runtime: RuntimeDependency | null; failed: boolean; error?: unknown }> {
  try {
    const runtime = await api.startRuntime();
    return {
      runtime,
      failed: runtime.status.health !== 'ok' && runtime.status.health !== 'degraded',
    };
  } catch (error) {
    // A failed start changes supervisor health. Read that authoritative state
    // back so the persistent page does not keep presenting lazy-start idleness.
    const runtime = await api.getRuntimeStatus().catch(() => null);
    return {
      runtime,
      failed: runtime?.status.health !== 'ok' && runtime?.status.health !== 'degraded',
      error,
    };
  }
}

export type InstallAndStartResult = {
  runtime: RuntimeDependency | null;
  failedStep: 'install' | 'start' | null;
  error?: unknown;
};

/** Resume the held install-and-start promise at its first unproven step. */
export async function resumeInstallAndStartRuntime(
  api: Pick<ModelsApi, 'installRuntime' | 'startRuntime' | 'getRuntimeStatus'>,
  initialRuntime: RuntimeDependency,
  onRuntime: (runtime: RuntimeDependency) => void = () => {},
  installPollIntervalMs = 2_000,
): Promise<InstallAndStartResult> {
  let runtime = initialRuntime;
  if (installAndStartStep(runtime) === 'install') {
    const installed = await installRuntimeUntilSettled(
      api,
      onRuntime,
      installPollIntervalMs,
      runtime.status.health === 'installing' ? runtime : undefined,
    );
    if (!installed.runtime || installed.failed) {
      return { runtime: installed.runtime, failedStep: 'install' };
    }
    runtime = installed.runtime;
  }

  if (installAndStartStep(runtime) === 'start') {
    const started = await startRuntimeWithStatusRefresh(api);
    if (started.runtime) onRuntime(started.runtime);
    if (!started.runtime || started.failed) {
      return { runtime: started.runtime, failedStep: 'start', error: started.error };
    }
    runtime = started.runtime;
  }

  return { runtime, failedStep: null };
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
