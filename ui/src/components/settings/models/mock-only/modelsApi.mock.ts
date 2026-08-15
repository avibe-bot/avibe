import mockCorpusJson from './modelHubMockCorpus.json';
import { buildMockEvents, buildMockRuntime } from './mockData';
import {
  ApiCallError,
  modelHubOperationRegistry,
  routeHopRefs,
  supplyGaps,
  type CredentialReplacement,
  type GuardConfirmation,
  type ModelHubOperation,
  type ModelsApi,
  type OAuthResult,
  type SourceCreated,
  type SourceDeleted,
  type SourcePatched,
  type SourceRefresh,
} from '../modelsApi';
import type {
  AgentBackend,
  AgentChain,
  AgentChainMutation,
  AgentChainPut,
  AgentMenu,
  AgentMode,
  AgentSourcesPut,
  AgentSupply,
  ApiKeySourceCreate,
  ApiKeySourceObservation,
  CredentialReplace,
  CustomModelCreate,
  MigrationApplyResult,
  MigrationScan,
  OAuthFlow,
  ProbeResult,
  ResolutionEvent,
  RuntimeDependency,
  Source,
  SourceObservation,
  SourcePatch,
  SupplyChannel,
} from '../types';

type JsonObject = Record<string, unknown>;
type RecordedBody =
  | { present: false }
  | { present: true; value: unknown };
type RecordedRequest = {
  operation: ModelHubOperation;
  path: JsonObject;
  query: Record<string, { present: boolean; value?: unknown }>;
  body: RecordedBody;
};
type RecordedReads = {
  sources: Source[];
  agents: AgentSupply[];
  agent_sources: Record<AgentBackend, AgentSupply>;
  agent_chains: Partial<Record<AgentBackend, Record<string, AgentChain>>>;
};
type RecordedOutcome =
  | { kind: 'success'; value: unknown }
  | {
      kind: 'error';
      error: string;
      detail: string;
      status: number;
      data: JsonObject;
    };
type RecordedTransition = {
  key: {
    id: string;
    pre: {
      model_hub_config_sha256: string;
      fixture_world_sha256: string;
    };
    request: RecordedRequest;
  };
  outcome: RecordedOutcome;
  post: {
    model_hub_config_sha256: string;
    fixture_world_sha256: string;
    config: unknown;
    fixture_world: unknown;
    reads: RecordedReads;
  };
};
type FieldPath = string[];
type RequestIdentity = {
  strategy: 'all_except_declared';
  sensitive_fields: FieldPath[];
  sensitive_placeholder: string;
  volatile_fields: FieldPath[];
  volatile_placeholder: string;
};
type OperationRegistration = {
  operation: ModelHubOperation;
  dispatch: 'authoritative_server' | 'unrecordable';
  recording: {
    command: string | null;
    request: RecordedRequest;
    proven_transitions: { id: string; request_token: string }[];
    unproven_reason: string;
  };
  reachability:
    | { kind: 'seed'; prerequisites: []; reason: null }
    | { kind: 'sequence'; prerequisites: RecordedRequest[]; reason: null }
    | { kind: 'unrecordable'; prerequisites: []; reason: string };
  request_identity: RequestIdentity;
};

export type MockCorpus = {
  artifact: 'model-hub-mock-corpus-v1';
  operation_registry: OperationRegistration[];
  seed: {
    model_hub_config_sha256: string;
    fixture_world_sha256: string;
    config: unknown;
    fixture_world: unknown;
    reads: RecordedReads;
  };
  transitions: RecordedTransition[];
};

const mockCorpus = mockCorpusJson as unknown as MockCorpus;
const clone = <T>(value: T): T => structuredClone(value);

const canonicalJson = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    return `{${Object.entries(value as JsonObject)
      .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
      .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`)
      .join(',')}}`;
  }
  return JSON.stringify(value);
};

const sha256 = async (value: string): Promise<string> => {
  const digest = await globalThis.crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0'))
    .join('');
};

const normalizedRequest = (
  operation: ModelHubOperation,
  path: JsonObject = {},
  body: RecordedBody = { present: false },
  query: RecordedRequest['query'] = {},
): RecordedRequest => ({ operation, path, query, body });

const replaceField = (value: JsonObject, path: FieldPath, replacement: string): void => {
  let parent: unknown = value;
  for (const member of path.slice(0, -1)) {
    if (parent === null || typeof parent !== 'object' || !(member in parent)) return;
    parent = (parent as JsonObject)[member];
  }
  const member = path.at(-1);
  if (member && parent !== null && typeof parent === 'object' && member in parent) {
    (parent as JsonObject)[member] = replacement;
  }
};

const fieldValue = (value: JsonObject, path: FieldPath): { present: boolean; value?: unknown } => {
  let current: unknown = value;
  for (const member of path) {
    if (current === null || typeof current !== 'object' || !(member in current)) {
      return { present: false };
    }
    current = (current as JsonObject)[member];
  }
  return { present: true, value: current };
};

class VolatileAliases {
  private readonly values = new Map<string, Map<string, string>>();
  private readonly next = new Map<string, number>();

  alias(
    operation: ModelHubOperation,
    path: FieldPath,
    value: unknown,
    template: string,
  ): string {
    const scope = `${operation}\u0000${path.join('\u0000')}`;
    const existing = typeof value === 'string'
      ? /^<volatile:([1-9][0-9]*)>$/.exec(value)
      : null;
    const values = this.values.get(scope) ?? new Map<string, string>();
    this.values.set(scope, values);
    if (existing) {
      this.next.set(scope, Math.max(this.next.get(scope) ?? 1, Number(existing[1]) + 1));
      values.set(canonicalJson(value), value as string);
      return value as string;
    }

    const identity = canonicalJson(value);
    const known = values.get(identity);
    if (known) return known;
    const index = this.next.get(scope) ?? 1;
    const alias = template.replace('{index}', String(index));
    values.set(identity, alias);
    this.next.set(scope, index + 1);
    return alias;
  }
}

const canonicalRequest = (
  request: RecordedRequest,
  identity: RequestIdentity,
  aliases: VolatileAliases,
): RecordedRequest => {
  const canonical = clone(request) as RecordedRequest & JsonObject;
  for (const path of identity.sensitive_fields) {
    replaceField(canonical, path, identity.sensitive_placeholder);
  }
  for (const path of identity.volatile_fields) {
    const field = fieldValue(canonical, path);
    if (field.present) {
      replaceField(
        canonical,
        path,
        aliases.alias(
          request.operation,
          path,
          field.value,
          identity.volatile_placeholder,
        ),
      );
    }
  }
  return canonical;
};

export class UncontractedMockTransitionError extends Error {
  readonly code = 'uncontracted_mock_transition';
  readonly missingKey: string;
  readonly operation: string;
  readonly generatorCommand: string | null;
  readonly canonicalRequest: string;
  readonly recordingReason: string | null;

  constructor(
    missingKey: string,
    operation: string,
    generatorCommand: string | null,
    request: RecordedRequest,
    recordingReason: string | null,
  ) {
    const renderedRequest = canonicalJson(request);
    super([
      'uncontracted_mock_transition',
      `Missing key: ${missingKey}`,
      `Operation: ${operation}`,
      `Canonical request: ${renderedRequest}`,
      generatorCommand
        ? `Record it: ${generatorCommand}`
        : `Not recordable: ${recordingReason ?? 'no authoritative server dispatch exists'}`,
    ].join('\n'));
    this.name = 'UncontractedMockTransitionError';
    this.missingKey = missingKey;
    this.operation = operation;
    this.generatorCommand = generatorCommand;
    this.canonicalRequest = renderedRequest;
    this.recordingReason = recordingReason;
  }
}

/**
 * A replay store, not a second Model Hub service.
 *
 * Every policy-bearing mutation must match a transition produced by the Python
 * service. Reads are the server projections recorded at that exact post-state.
 */
export class MockStore {
  private reads: RecordedReads;
  private configHash: string;
  private fixtureWorldHash: string;
  private readonly transitions: Map<string, RecordedTransition>;
  private readonly registrations: Map<ModelHubOperation, OperationRegistration>;
  private readonly volatileAliases = new VolatileAliases();
  private readonly events = buildMockEvents();
  private readonly runtime = buildMockRuntime();

  constructor(corpus: MockCorpus = mockCorpus) {
    if (corpus.artifact !== 'model-hub-mock-corpus-v1') {
      throw new Error('Unsupported Model Hub mock corpus artifact');
    }
    this.reads = clone(corpus.seed.reads);
    this.configHash = corpus.seed.model_hub_config_sha256;
    this.fixtureWorldHash = corpus.seed.fixture_world_sha256;
    this.transitions = new Map(
      corpus.transitions.map((transition) => [transition.key.id, transition]),
    );
    this.registrations = new Map(
      corpus.operation_registry.map((registration) => [registration.operation, registration]),
    );
  }

  get sources(): Source[] {
    return clone(this.reads.sources);
  }

  get agents(): AgentSupply[] {
    return clone(this.reads.agents);
  }

  private async transitionKey(request: RecordedRequest) {
    const registration = this.registrations.get(request.operation);
    if (!registration) {
      throw new UncontractedMockTransitionError(
        'unregistered-operation',
        request.operation,
        null,
        request,
        'no operation registry entry exists',
      );
    }
    const normalized = canonicalRequest(
      request,
      registration.request_identity,
      this.volatileAliases,
    );
    const input = {
      version: 2,
      pre: {
        model_hub_config_sha256: this.configHash,
        fixture_world_sha256: this.fixtureWorldHash,
      },
      request: normalized,
    };
    const rendered = canonicalJson(input);
    return {
      id: await sha256(rendered),
      request: normalized,
      registration,
    };
  }

  private replay<T>(request: RecordedRequest, signal?: AbortSignal): Promise<T> {
    let transition: RecordedTransition | undefined;
    return modelHubOperationRegistry[request.operation].execute(
      async () => {
        const key = await this.transitionKey(request);
        transition = this.transitions.get(key.id);
        if (!transition) {
          const proof = key.registration.recording.proven_transitions.find(
            (candidate) => candidate.id === key.id,
          );
          const generatorCommand = key.registration.recording.command && proof
            ? `${key.registration.recording.command} --record-miss ${key.id} --request-token ${proof.request_token}`
            : null;
          throw new UncontractedMockTransitionError(
            key.id,
            request.operation,
            generatorCommand,
            key.request,
            proof
              ? null
              : key.registration.recording.unproven_reason,
          );
        }

        if (transition.outcome.kind === 'error') {
          const data = transition.outcome.data;
          throw new ApiCallError(
            transition.outcome.error,
            transition.outcome.detail,
            true,
            supplyGaps(data.would_interrupt),
            supplyGaps(data.interrupted_pairs),
            routeHopRefs(data.would_remove_hops),
            transition.outcome.status,
            typeof data.observation === 'object' && data.observation !== null
              ? data.observation as SourceObservation
              : undefined,
          );
        }
        return clone(transition.outcome.value);
      },
      {
        signal,
        commit: () => {
          if (!transition) return;
          this.reads = clone(transition.post.reads);
          this.configHash = transition.post.model_hub_config_sha256;
          this.fixtureWorldHash = transition.post.fixture_world_sha256;
        },
      },
    ) as Promise<T>;
  }

  listSources(): Promise<Source[]> {
    return Promise.resolve(
      modelHubOperationRegistry.listSources.responseTransform({ sources: this.sources }),
    );
  }

  listAgents(): Promise<AgentSupply[]> {
    return Promise.resolve(
      modelHubOperationRegistry.listAgents.responseTransform({ agents: this.agents }),
    );
  }

  getAgentSources(backend: AgentBackend): Promise<AgentSupply> {
    const projection = this.reads.agent_sources[backend];
    if (projection) {
      return Promise.resolve(
        modelHubOperationRegistry.getAgentSources.responseTransform({ agent: clone(projection) }),
      );
    }
    return this.replay(normalizedRequest('getAgentSources', { backend }));
  }

  getAgentChain(backend: AgentBackend, model: string): Promise<AgentChain> {
    const projection = this.reads.agent_chains[backend]?.[model];
    if (projection) {
      return Promise.resolve(
        modelHubOperationRegistry.getAgentChain.responseTransform({ chain: clone(projection) }),
      );
    }
    return this.replay(normalizedRequest(
      'getAgentChain',
      { backend },
      { present: false },
      { model: { present: true, value: model } },
    ));
  }

  listEvents(limit = 20, before?: string): Promise<ResolutionEvent[]> {
    const start = before
      ? Math.max(0, this.events.findIndex((event) => event.id === before) + 1)
      : 0;
    return Promise.resolve(
      modelHubOperationRegistry.listEvents.responseTransform({
        events: clone(this.events.slice(start, start + limit)),
      }),
    );
  }

  getRuntimeStatus(): Promise<RuntimeDependency> {
    return Promise.resolve(
      modelHubOperationRegistry.getRuntimeStatus.responseTransform({ runtime: clone(this.runtime) }),
    );
  }

  observeApiKeySource(
    draft: ApiKeySourceObservation,
    signal?: AbortSignal,
  ): Promise<SourceObservation> {
    return this.replay(normalizedRequest(
      'observeApiKeySource',
      {},
      { present: true, value: draft },
    ), signal);
  }

  createApiKeySource(draft: ApiKeySourceCreate): Promise<SourceCreated> {
    return this.replay(normalizedRequest(
      'createApiKeySource',
      {},
      { present: true, value: draft },
    ));
  }

  patchSource(id: string, body: SourcePatch): Promise<SourcePatched> {
    return this.replay(normalizedRequest(
      'patchSource',
      { id },
      { present: true, value: body },
    ));
  }

  refreshSource(
    id: string,
    confirmation?: GuardConfirmation,
  ): Promise<SourceRefresh> {
    return this.replay(normalizedRequest(
      'refreshSource',
      { id },
      { present: true, value: confirmation ?? {} },
    ));
  }

  deleteSource(
    id: string,
    confirmation?: GuardConfirmation,
  ): Promise<SourceDeleted> {
    return this.replay(normalizedRequest(
      'deleteSource',
      { id },
      confirmation
        ? {
            present: true,
            value: {
              would_remove_hops: confirmation.would_remove_hops,
              would_interrupt: confirmation.would_interrupt,
            },
          }
        : { present: false },
      confirmation
        ? { force: { present: true, value: true } }
        : {},
    ));
  }

  replaceCredential(
    id: string,
    body: CredentialReplace,
  ): Promise<CredentialReplacement> {
    return this.replay(normalizedRequest(
      'replaceCredential',
      { id },
      { present: true, value: body },
    ));
  }

  reauthSource(id: string): Promise<OAuthFlow> {
    return this.replay(normalizedRequest(
      'reauthSource',
      { id },
      { present: true, value: { acknowledge_irreversible: true } },
    ));
  }

  putAgentSources(backend: AgentBackend, body: AgentSourcesPut): Promise<AgentSupply> {
    return this.replay(normalizedRequest(
      'putAgentSources',
      { backend },
      { present: true, value: body },
    ));
  }

  putAgentChain(
    backend: AgentBackend,
    model: string,
    body: AgentChainPut,
  ): Promise<AgentChainMutation> {
    return this.replay(normalizedRequest(
      'putAgentChain',
      { backend },
      { present: true, value: body },
      { model: { present: true, value: model } },
    ));
  }

  probeAgent(backend: AgentBackend, model?: string): Promise<ProbeResult> {
    return this.replay(normalizedRequest(
      'probeAgent',
      { backend },
      { present: true, value: model ? { model } : {} },
    ));
  }

  setAgentMode(backend: AgentBackend, mode: AgentMode): Promise<AgentSupply> {
    return this.replay(normalizedRequest(
      'setAgentMode',
      { backend },
      { present: true, value: { mode } },
    ));
  }

  putMenu(menu: AgentMenu): Promise<AgentSupply> {
    return this.replay(normalizedRequest(
      'putMenu',
      { backend: 'opencode' },
      { present: true, value: { menu } },
    ));
  }

  addCustomModel(sourceId: string, draft: CustomModelCreate): Promise<Source> {
    return this.replay(normalizedRequest(
      'addCustomModel',
      { sourceId },
      { present: true, value: draft },
    ));
  }

  updateModelReasoningEfforts(
    sourceId: string,
    modelId: string,
    reasoningEfforts: string[],
  ): Promise<Source> {
    return this.replay(normalizedRequest(
      'updateModelReasoningEfforts',
      { sourceId, modelId },
      { present: true, value: { reasoning_efforts: reasoningEfforts } },
    ));
  }

  deleteCustomModel(
    sourceId: string,
    modelId: string,
    confirmation?: GuardConfirmation,
  ): Promise<Source> {
    return this.replay(normalizedRequest(
      'deleteCustomModel',
      { sourceId, modelId },
      { present: true, value: confirmation ?? {} },
    ));
  }

  scanMigration(): Promise<MigrationScan> {
    return this.replay(normalizedRequest('scanMigration'));
  }

  applyMigration(itemIds: string[]): Promise<MigrationApplyResult> {
    return this.replay(normalizedRequest(
      'applyMigration',
      {},
      { present: true, value: { item_ids: itemIds } },
    ));
  }

  installRuntime(): Promise<RuntimeDependency> {
    return this.replay(normalizedRequest('installRuntime'));
  }

  startRuntime(): Promise<RuntimeDependency> {
    return this.replay(normalizedRequest('startRuntime'));
  }

  startOAuth(
    vendor: string,
    channel: SupplyChannel,
    clientNonce?: string,
  ): Promise<OAuthFlow> {
    return this.replay(normalizedRequest(
      'startOAuth',
      {},
      {
        present: true,
        value: {
          vendor,
          channel,
          ...(clientNonce ? { client_nonce: clientNonce } : {}),
        },
      },
    ));
  }

  getOAuthStatus(flowId: string): Promise<OAuthResult> {
    return this.replay(normalizedRequest('getOAuthStatus', { flowId }));
  }

  submitOAuth(flowId: string, value: string): Promise<OAuthResult> {
    return this.replay(normalizedRequest(
      'submitOAuth',
      {},
      { present: true, value: { flow_id: flowId, value } },
    ));
  }

  cancelOAuth(flowId: string): Promise<void> {
    return this.replay(normalizedRequest(
      'cancelOAuth',
      {},
      { present: true, value: { flow_id: flowId } },
    ));
  }
}

export const createMockApi = (): ModelsApi => {
  const store = new MockStore();
  return {
    listSources: () => store.listSources(),
    observeApiKeySource: (draft, signal) => store.observeApiKeySource(draft, signal),
    createApiKeySource: (draft) => store.createApiKeySource(draft),
    patchSource: (id, patch) => store.patchSource(id, patch),
    refreshSource: (id, confirmation) => store.refreshSource(id, confirmation),
    deleteSource: (id, confirmation) => store.deleteSource(id, confirmation),
    replaceCredential: (id, body) => store.replaceCredential(id, body),
    reauthSource: (id) => store.reauthSource(id),
    listAgents: () => store.listAgents(),
    getAgentSources: (backend) => store.getAgentSources(backend),
    putAgentSources: (backend, body) => store.putAgentSources(backend, body),
    getAgentChain: (backend, model) => store.getAgentChain(backend, model),
    putAgentChain: (backend, model, body) => store.putAgentChain(backend, model, body),
    probeAgent: (backend, model) => store.probeAgent(backend, model),
    setAgentMode: (backend, mode) => store.setAgentMode(backend, mode),
    putMenu: (menu) => store.putMenu(menu),
    addCustomModel: (sourceId, draft) => store.addCustomModel(sourceId, draft),
    updateModelReasoningEfforts: (sourceId, modelId, efforts) =>
      store.updateModelReasoningEfforts(sourceId, modelId, efforts),
    deleteCustomModel: (sourceId, modelId, confirmation) =>
      store.deleteCustomModel(sourceId, modelId, confirmation),
    scanMigration: () => store.scanMigration(),
    applyMigration: (itemIds) => store.applyMigration(itemIds),
    listEvents: (limit, before) => store.listEvents(limit, before),
    getRuntimeStatus: () => store.getRuntimeStatus(),
    installRuntime: () => store.installRuntime(),
    startRuntime: () => store.startRuntime(),
    startOAuth: (vendor, channel, nonce) => store.startOAuth(vendor, channel, nonce),
    getOAuthStatus: (flowId) => store.getOAuthStatus(flowId),
    submitOAuth: (flowId, value) => store.submitOAuth(flowId, value),
    cancelOAuth: (flowId) => store.cancelOAuth(flowId),
  };
};
