import { apiFailure, type ModelsApi } from './modelsApi';
import { installRuntimeUntilSettled, runtimeIsRunning } from './runtimeLifecycle';
import type { AgentBackend, AgentSupply, RuntimeDependency } from './types';

export type GatewayAdoptionFailure = {
  step: 'install' | 'start' | 'mode' | 'read';
  request?: string;
  responseStatus?: number;
  reason: 'transport' | 'refused' | 'notReady' | 'unknown';
};

export type GatewayAdoptionResult =
  | { ok: true; agent: AgentSupply; runtime: RuntimeDependency | null }
  | { ok: false; failure: GatewayAdoptionFailure; runtime: RuntimeDependency | null };

const classifiedFailure = (
  error: unknown,
  step: GatewayAdoptionFailure['step'],
  request?: string,
): GatewayAdoptionFailure => {
  const failure = apiFailure(error);
  if (!failure || !failure.serverNamed) {
    return { step, request, responseStatus: failure?.responseStatus, reason: 'transport' };
  }
  if (failure.code === 'engine_down' || failure.code === 'runtime_not_ready') {
    return { step, request, responseStatus: failure.responseStatus, reason: 'notReady' };
  }
  if (failure.responseStatus && failure.responseStatus >= 400 && failure.responseStatus < 500) {
    return { step, request, responseStatus: failure.responseStatus, reason: 'refused' };
  }
  return { step, request, responseStatus: failure.responseStatus, reason: 'unknown' };
};

const readFailure = (error: unknown, request: string): GatewayAdoptionResult => ({
  ok: false,
  failure: classifiedFailure(error, 'read', request),
  runtime: null,
});

const backendRow = (agents: AgentSupply[], backend: AgentBackend): AgentSupply | null =>
  agents.find((agent) => agent.backend === backend) ?? null;

/**
 * Reconciles before every attempt, so retry resumes at the first unproven step.
 * Installation, startup, and mode adoption are separate proven steps. A retry
 * resumes at the first step the server state has not already confirmed.
 */
export async function resumeGatewayAdoption(
  api: Pick<ModelsApi, 'listAgents' | 'getRuntimeStatus' | 'installRuntime' | 'startRuntime' | 'setAgentMode'>,
  backend: AgentBackend,
  installPollIntervalMs = 2_000,
): Promise<GatewayAdoptionResult> {
  let agents: AgentSupply[];
  try {
    agents = await api.listAgents();
  } catch (error) {
    return readFailure(error, 'GET /api/models/agents');
  }

  const current = backendRow(agents, backend);
  if (!current) {
    return {
      ok: false,
      failure: { step: 'read', request: 'GET /api/models/agents', reason: 'unknown' },
      runtime: null,
    };
  }
  if (current.mode === 'hub') return { ok: true, agent: current, runtime: null };

  let runtime: RuntimeDependency;
  try {
    runtime = await api.getRuntimeStatus();
  } catch (error) {
    return readFailure(error, 'GET /api/models/runtime/status');
  }

  if (runtime.status.health === 'not_installed' || runtime.status.health === 'installing') {
    const installed = await installRuntimeUntilSettled(
      api,
      () => {},
      installPollIntervalMs,
      runtime.status.health === 'installing' ? runtime : undefined,
    );
    if (installed.runtime) runtime = installed.runtime;
    if (installed.failed) {
      return {
        ok: false,
        failure: { step: 'install', reason: 'unknown' },
        runtime: installed.runtime ?? runtime,
      };
    }
  }

  if (!runtimeIsRunning(runtime)) {
    try {
      runtime = await api.startRuntime();
    } catch (error) {
      const observed = await api.getRuntimeStatus().catch(() => null);
      if (observed && runtimeIsRunning(observed)) {
        runtime = observed;
      } else {
        return {
          ok: false,
          failure: classifiedFailure(error, 'start', 'POST /api/models/runtime/start'),
          runtime: observed ?? runtime,
        };
      }
    }
  }

  if (!runtimeIsRunning(runtime)) {
    return {
      ok: false,
      failure: { step: 'start', request: 'POST /api/models/runtime/start', reason: 'notReady' },
      runtime,
    };
  }

  try {
    const agent = await api.setAgentMode(backend, 'hub');
    return { ok: true, agent, runtime };
  } catch (error) {
    try {
      agents = await api.listAgents();
      const reconciled = backendRow(agents, backend);
      if (reconciled?.mode === 'hub') return { ok: true, agent: reconciled, runtime };
    } catch {
      // The write failure remains the best evidence; retry will run both reads.
    }
    return {
      ok: false,
      failure: classifiedFailure(error, 'mode', `PATCH /api/models/agents/${backend}/mode`),
      runtime,
    };
  }
}
