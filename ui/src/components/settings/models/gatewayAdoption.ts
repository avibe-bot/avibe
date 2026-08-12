import { apiFailure, type ModelsApi } from './modelsApi';
import type { CollectionReadAuthority } from './collectionReadAuthority';
import { resumeInstallAndStartRuntime } from './runtimeLifecycle';
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
  api: Pick<ModelsApi, 'getRuntimeStatus' | 'installRuntime' | 'startRuntime' | 'setAgentMode'>,
  agentReads: CollectionReadAuthority<AgentSupply[]>,
  backend: AgentBackend,
  installPollIntervalMs = 2_000,
): Promise<GatewayAdoptionResult> {
  let agents: AgentSupply[];
  try {
    agents = await agentReads.readValue();
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

  const runtimeSequence = await resumeInstallAndStartRuntime(api, runtime, () => {}, installPollIntervalMs);
  if (runtimeSequence.runtime) runtime = runtimeSequence.runtime;
  if (runtimeSequence.failedStep === 'install') {
    return {
      ok: false,
      failure: { step: 'install', reason: 'unknown' },
      runtime,
    };
  }
  if (runtimeSequence.failedStep === 'start') {
    return {
      ok: false,
      failure: runtimeSequence.error
        ? classifiedFailure(runtimeSequence.error, 'start', 'POST /api/models/runtime/start')
        : { step: 'start', request: 'POST /api/models/runtime/start', reason: 'notReady' },
      runtime,
    };
  }

  try {
    const agent = await api.setAgentMode(backend, 'hub');
    return { ok: true, agent, runtime };
  } catch (error) {
    try {
      agents = await agentReads.readValue();
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
