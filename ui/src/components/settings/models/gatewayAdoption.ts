import { apiFailure, type ModelsApi } from './modelsApi';
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

const running = (runtime: RuntimeDependency): boolean =>
  runtime.status.health === 'ok' || runtime.status.health === 'degraded';

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
 * The contracted start route is the only runtime mutation: no install endpoint
 * is invented for G-10's client-side installing state.
 */
export async function resumeGatewayAdoption(
  api: Pick<ModelsApi, 'listAgents' | 'getRuntimeStatus' | 'startRuntime' | 'setAgentMode'>,
  backend: AgentBackend,
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

  if (!running(runtime)) {
    const startedMissing = runtime.status.health === 'not_installed';
    try {
      runtime = await api.startRuntime();
    } catch (error) {
      const observed = await api.getRuntimeStatus().catch(() => null);
      if (observed && running(observed)) {
        runtime = observed;
      } else {
        const stillMissing = observed?.status.health === 'not_installed' || (!observed && startedMissing);
        return {
          ok: false,
          failure: stillMissing
            ? { step: 'install', reason: classifiedFailure(error, 'install').reason }
            : classifiedFailure(error, 'start', 'POST /api/models/runtime/start'),
          runtime: observed ?? runtime,
        };
      }
    }
  }

  if (!running(runtime)) {
    return {
      ok: false,
      failure: runtime.status.health === 'not_installed'
        ? { step: 'install', reason: 'unknown' }
        : { step: 'start', request: 'POST /api/models/runtime/start', reason: 'notReady' },
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
