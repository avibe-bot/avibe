import { execSync } from 'node:child_process';
import {
  copyFileSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

import { describe, expect, it, vi } from 'vitest';

import mockCorpusJson from './mock-only/modelHubMockCorpus.json';
import { ApiCallError, modelHubOperationRegistry } from './modelsApi';
import {
  MockStore,
  UncontractedMockTransitionError,
  type MockCorpus,
} from './mock-only/modelsApi.mock';
import type { RouteHopRef } from './types';

const referencesTo = (store: MockStore, sourceId: string): RouteHopRef[] =>
  store.agents.flatMap((agent) =>
    Object.entries(agent.routes ?? {}).flatMap(([menuModel, route]) =>
      route.hops.flatMap((hop, index) => hop.source_id === sourceId
        ? [{
            backend: agent.backend,
            menu_model: menuModel,
            source_id: hop.source_id,
            model_id: hop.model_id,
            position: index + 1,
          }]
        : [])));

describe('Model Hub mock deletion contract', () => {
  it('reports and removes every seeded route reference to a deleted source', async () => {
    const seeded = new MockStore();
    const routedSourceIds = new Set(
      seeded.agents.flatMap((agent) =>
        Object.values(agent.routes ?? {}).flatMap((route) =>
          route.hops.map((hop) => hop.source_id))),
    );
    expect(routedSourceIds.size).toBeGreaterThan(0);

    for (const sourceId of routedSourceIds) {
      const store = new MockStore();
      const expected = referencesTo(store, sourceId);
      let refusal: ApiCallError | null = null;
      try {
        await store.deleteSource(sourceId);
      } catch (error) {
        if (error instanceof ApiCallError) refusal = error;
        else throw error;
      }

      expect(refusal, sourceId).not.toBeNull();
      expect(refusal?.code, sourceId).toBe('source_in_route_chain');
      expect(refusal?.wouldRemoveHops, sourceId).toEqual(expected);
      const answer = await store.deleteSource(sourceId, {
        force: true,
        would_remove_hops: refusal?.wouldRemoveHops ?? [],
        would_interrupt: refusal?.wouldInterrupt ?? [],
      });

      expect(answer.removed_hops, sourceId).toEqual(expected);
      expect(referencesTo(store, sourceId), sourceId).toEqual([]);
      for (const agent of store.agents) {
        expect(agent.sources?.order ?? [], `${sourceId}:${agent.backend}`).not.toContain(sourceId);
      }
    }
  });
});

describe('Model Hub mock endpoint-edit contract', () => {
  it('reports and removes every invalidated route reference for every seeded routed Hub key', async () => {
    const seeded = new MockStore();
    const routedSourceIds = new Set(
      seeded.agents.flatMap((agent) =>
        Object.values(agent.routes ?? {}).flatMap((route) =>
          route.hops.map((hop) => hop.source_id))),
    );
    const editableRoutedSourceIds = seeded.sources
      .filter((source) => routedSourceIds.has(source.id)
        && source.kind === 'api_key'
        && source.supply_channel === 'hub'
        && source.credential_ref)
      .map((source) => source.id);
    expect(editableRoutedSourceIds.length).toBeGreaterThan(0);

    for (const sourceId of editableRoutedSourceIds) {
      const store = new MockStore();
      const beforeReferences = referencesTo(store, sourceId);
      const beforeRoutes = new Map(store.agents.flatMap((agent) =>
        Object.entries(agent.routes ?? {}).map(([menuModel, route]) => [
          `${agent.backend}\u0000${menuModel}`,
          route.hops.map((hop) => ({ ...hop })),
        ])));
      const beforeOrders = new Map(store.agents.map((agent) => [
        agent.backend,
        [...(agent.sources?.order ?? [])],
      ]));
      const base_url = `https://retarget-${sourceId}.example/v1`;
      let refusal: ApiCallError | null = null;
      let answer;
      try {
        answer = await store.patchSource(sourceId, { base_url });
      } catch (error) {
        if (error instanceof ApiCallError) refusal = error;
        else throw error;
      }
      if (refusal) {
        expect(refusal.code, sourceId).toBe(refusal.wouldRemoveHops.length > 0
          ? 'source_model_in_route_chain'
          : 'source_last_supplier');
        answer = await store.patchSource(sourceId, {
          base_url,
          force: true,
          would_remove_hops: refusal.wouldRemoveHops,
          would_interrupt: refusal.wouldInterrupt,
        });
      }
      expect(answer, sourceId).toBeDefined();
      const result = answer!;
      const candidateModelIds = new Set(result.source.models
        .filter((model) => model.retired !== true)
        .map((model) => model.id));
      const expectedRemoved = beforeReferences.filter((hop) => !candidateModelIds.has(hop.model_id));
      expect(result.removed_hops, sourceId).toEqual(expectedRemoved);
      expect(refusal?.wouldRemoveHops ?? [], sourceId).toEqual(expectedRemoved);

      const removed = new Set(expectedRemoved.map((hop) =>
        `${hop.backend}\u0000${hop.menu_model}\u0000${hop.source_id}\u0000${hop.model_id}`));
      for (const agent of store.agents) {
        expect(agent.sources?.order ?? [], `${sourceId}:${agent.backend}:order`)
          .toEqual(beforeOrders.get(agent.backend));
        for (const [menuModel, route] of Object.entries(agent.routes ?? {})) {
          const before = beforeRoutes.get(`${agent.backend}\u0000${menuModel}`) ?? [];
          expect(route.hops, `${sourceId}:${agent.backend}:${menuModel}:route`).toEqual(
            before.filter((hop) =>
              !removed.has(`${agent.backend}\u0000${menuModel}\u0000${hop.source_id}\u0000${hop.model_id}`)),
          );
          for (const hop of route.hops.filter((hop) => hop.source_id === sourceId)) {
            expect(candidateModelIds.has(hop.model_id), `${sourceId}:${agent.backend}:${menuModel}:${hop.model_id}`).toBe(true);
          }
        }
      }
    }
  });
});

describe('Model Hub mock replay boundary', () => {
  it('keys a delete by the full state after source reordering', async () => {
    const store = new MockStore();
    const reordered = ['src_anthkey01', 'src_claudepro1', 'src_relay9c1x'];
    await store.putAgentSources('claude', { order: reordered });
    expect((await store.getAgentSources('claude')).sources?.order).toEqual(reordered);

    let refusal: ApiCallError | null = null;
    try {
      await store.deleteSource('src_claudepro1');
    } catch (error) {
      if (error instanceof ApiCallError) refusal = error;
      else throw error;
    }
    expect(refusal?.code).toBe('source_in_route_chain');
    await store.deleteSource('src_claudepro1', {
      force: true,
      would_remove_hops: refusal?.wouldRemoveHops ?? [],
      would_interrupt: refusal?.wouldInterrupt ?? [],
    });
    expect((await store.getAgentSources('claude')).sources?.order)
      .toEqual(['src_anthkey01', 'src_relay9c1x']);
  });

  it('runs every registered recovery path and applies the shared response transform', async () => {
    const corpus = mockCorpusJson as unknown as MockCorpus;
    const nonMutationMembers = new Set([
      'constructor',
      'sources',
      'agents',
      'transitionKey',
      'replay',
      'listSources',
      'listAgents',
      'listEvents',
      'getRuntimeStatus',
    ]);
    const reachableMutations = Object.getOwnPropertyNames(MockStore.prototype)
      .filter((name) => !nonMutationMembers.has(name))
      .sort();
    const apiMethods = Object.getOwnPropertyNames(MockStore.prototype)
      .filter((name) => ![
        'constructor',
        'sources',
        'agents',
        'transitionKey',
        'replay',
      ].includes(name))
      .sort();
    expect(Object.keys(modelHubOperationRegistry).sort()).toEqual(apiMethods);
    const registered = corpus.operation_registry
      .map(({ operation }) => operation)
      .sort();
    expect(reachableMutations).toEqual(registered);
    for (const entry of corpus.operation_registry) {
      expect(entry.recording.request.operation, entry.operation).toBe(entry.operation);
      expect(entry.request_identity.strategy, entry.operation).toBe('all_except_declared');
      expect(Array.isArray(entry.request_identity.sensitive_fields), entry.operation).toBe(true);
      expect(Array.isArray(entry.request_identity.volatile_fields), entry.operation).toBe(true);
      expect(
        typeof modelHubOperationRegistry[entry.operation].responseTransform,
        entry.operation,
      ).toBe('function');
      expect(
        typeof modelHubOperationRegistry[entry.operation].execute,
        entry.operation,
      ).toBe('function');
      if (entry.dispatch === 'unrecordable') {
        expect(entry.recording.command, entry.operation).toBeNull();
        expect(entry.reachability.kind, entry.operation).toBe('unrecordable');
        expect(entry.reachability.reason, entry.operation).toBeTruthy();
        expect(entry.recording.proven_transitions, entry.operation).toEqual([]);
        continue;
      }
      expect(entry.recording.command, entry.operation).toBe(
        'python3 scripts/generate_model_hub_mock_corpus.py',
      );
      expect(entry.recording.proven_transitions, entry.operation).toHaveLength(1);
      expect(
        entry.reachability.kind === 'seed'
          ? entry.reachability.prerequisites
          : entry.reachability.prerequisites.length,
        entry.operation,
      ).toEqual(entry.reachability.kind === 'seed' ? [] : expect.any(Number));
      if (entry.reachability.kind === 'sequence') {
        expect(entry.reachability.prerequisites.length, entry.operation).toBeGreaterThan(0);
      }
    }

    const repoRoot = resolve(import.meta.dirname, '../../../../..');
    const tempRoot = mkdtempSync(join(tmpdir(), 'model-hub-record-miss-'));
    type RegistryEntry = MockCorpus['operation_registry'][number];
    type Request = RegistryEntry['recording']['request'];
    const replayRequest = async (store: MockStore, request: Request) => {
      const replay = (
        store as unknown as {
          replay<T>(candidate: Request): Promise<T>;
        }
      ).replay.bind(store);
      try {
        return { kind: 'success' as const, value: await replay<unknown>(request) };
      } catch (error) {
        return { kind: 'error' as const, error };
      }
    };

    try {
      for (const entry of corpus.operation_registry) {
        if (entry.dispatch === 'unrecordable') {
          const store = new MockStore(corpus);
          const missing = await replayRequest(store, entry.recording.request);
          expect(missing.kind, entry.operation).toBe('error');
          const error = missing.kind === 'error' ? missing.error : null;
          expect(error, entry.operation).toBeInstanceOf(UncontractedMockTransitionError);
          expect((error as UncontractedMockTransitionError).generatorCommand).toBeNull();
          expect((error as UncontractedMockTransitionError).recordingReason).toBeTruthy();
          continue;
        }
        const operationRoot = join(tempRoot, entry.operation);
        mkdirSync(operationRoot);
        const seedPath = join(operationRoot, 'seed.json');
        const sequencesPath = join(operationRoot, 'sequences.json');
        const outputPath = join(operationRoot, 'corpus.json');
        copyFileSync(join(repoRoot, 'scripts/model_hub_mock_seed.json'), seedPath);
        copyFileSync(
          join(repoRoot, 'scripts/model_hub_mock_sequences.json'),
          sequencesPath,
        );

        let activeCorpus = corpus;
        const prefix: Request[] = [];
        const recoveryPath = [
          ...entry.reachability.prerequisites,
          entry.recording.request,
        ];
        for (const [index, request] of recoveryPath.entries()) {
          const store = new MockStore(activeCorpus);
          for (const previous of prefix) {
            const previousResult = await replayRequest(store, previous);
            expect(previousResult.kind, `${entry.operation}:prerequisite`).toBe('success');
          }

          const missing = await replayRequest(store, request);
          expect(missing.kind, `${entry.operation}:${request.operation}`).toBe('error');
          const error = missing.kind === 'error' ? missing.error : null;
          expect(error, `${entry.operation}:${request.operation}`).toBeInstanceOf(
            UncontractedMockTransitionError,
          );
          const uncontracted = error as UncontractedMockTransitionError;
          expect(uncontracted.operation, entry.operation).toBe(request.operation);
          expect(uncontracted.generatorCommand, entry.operation).toMatch(
            new RegExp(`^${entry.recording.command} --record-miss ${uncontracted.missingKey} --request-token [A-Za-z0-9_-]+$`),
          );

          execSync(uncontracted.generatorCommand ?? '', {
            cwd: repoRoot,
            env: {
              ...process.env,
              MODEL_HUB_MOCK_SEED_PATH: seedPath,
              MODEL_HUB_MOCK_SEQUENCES_PATH: sequencesPath,
              MODEL_HUB_MOCK_OUTPUT_PATH: outputPath,
            },
            stdio: 'pipe',
          });
          activeCorpus = JSON.parse(readFileSync(outputPath, 'utf8')) as MockCorpus;
          const recordedStore = new MockStore(activeCorpus);
          for (const previous of prefix) {
            const previousResult = await replayRequest(recordedStore, previous);
            expect(previousResult.kind, `${entry.operation}:recorded prerequisite`).toBe('success');
          }
          const recorded = await replayRequest(recordedStore, request);
          if (index < recoveryPath.length - 1) {
            expect(recorded.kind, `${entry.operation}:${request.operation}`).toBe('success');
            prefix.push(request);
            continue;
          }

          const transition = activeCorpus.transitions.find(
            (candidate) => candidate.key.id === uncontracted.missingKey,
          );
          expect(transition, entry.operation).toBeDefined();
          if (transition?.outcome.kind === 'success') {
            expect(recorded.kind, entry.operation).toBe('success');
            const expected = modelHubOperationRegistry[entry.operation]
              .responseTransform(structuredClone(transition.outcome.value));
            expect(
              recorded.kind === 'success' ? recorded.value : undefined,
              entry.operation,
            ).toEqual(expected);
          } else {
            expect(recorded.kind, entry.operation).toBe('error');
            const recordedError = recorded.kind === 'error' ? recorded.error : null;
            expect(recordedError, entry.operation).toBeInstanceOf(ApiCallError);
            expect((recordedError as ApiCallError).code, entry.operation).toBe(
              transition?.outcome.kind === 'error' ? transition.outcome.error : undefined,
            );
          }
        }
      }
    } finally {
      rmSync(tempRoot, { recursive: true, force: true });
    }
  }, 180_000);

  it('derives secret redaction, volatile aliases, and recording proofs from the registry', async () => {
    const corpus = mockCorpusJson as unknown as MockCorpus;
    type RegistryEntry = MockCorpus['operation_registry'][number];
    type Request = RegistryEntry['recording']['request'];
    type KeyResult = { id: string };
    const replay = (store: MockStore, request: Request) => (
      store as unknown as { replay<T>(candidate: Request): Promise<T> }
    ).replay<unknown>(request);
    const transitionKey = (store: MockStore, request: Request) => (
      store as unknown as { transitionKey(candidate: Request): Promise<KeyResult> }
    ).transitionKey(request);
    const setField = (request: Request, path: string[], value: string) => {
      let parent = request as unknown as Record<string, unknown>;
      for (const member of path.slice(0, -1)) {
        parent = parent[member] as Record<string, unknown>;
      }
      parent[path.at(-1)!] = value;
    };

    for (const transition of corpus.transitions) {
      expect(transition.key.id).toMatch(/^[0-9a-f]{64}$/);
    }

    for (const entry of corpus.operation_registry) {
      for (const [index, path] of entry.request_identity.sensitive_fields.entries()) {
        const sensitive = `sensitive-${entry.operation}-${index}`;
        const request = structuredClone(entry.recording.request);
        setField(request, path, sensitive);
        let failure: UncontractedMockTransitionError | null = null;
        try {
          await replay(new MockStore(corpus), request);
        } catch (error) {
          if (error instanceof UncontractedMockTransitionError) failure = error;
          else throw error;
        }
        expect(failure, entry.operation).not.toBeNull();
        expect(failure?.missingKey, entry.operation).not.toContain(sensitive);
        expect(failure?.canonicalRequest, entry.operation).not.toContain(sensitive);
        expect(failure?.generatorCommand ?? '', entry.operation).not.toContain(sensitive);
      }

      for (const [index, path] of entry.request_identity.volatile_fields.entries()) {
        const first = structuredClone(entry.recording.request);
        const second = structuredClone(entry.recording.request);
        setField(first, path, `volatile-a-${index}`);
        setField(second, path, `volatile-b-${index}`);
        expect(
          (await transitionKey(new MockStore(corpus), first)).id,
          entry.operation,
        ).toBe((await transitionKey(new MockStore(corpus), second)).id);
        const sequenceStore = new MockStore(corpus);
        expect(
          (await transitionKey(sequenceStore, first)).id,
          entry.operation,
        ).not.toBe((await transitionKey(sequenceStore, second)).id);
      }

      const unproven = structuredClone(entry.recording.request);
      unproven.path.__unproven = entry.operation;
      let failure: UncontractedMockTransitionError | null = null;
      try {
        await replay(new MockStore(corpus), unproven);
      } catch (error) {
        if (error instanceof UncontractedMockTransitionError) failure = error;
        else throw error;
      }
      expect(failure, entry.operation).not.toBeNull();
      expect(failure?.generatorCommand, entry.operation).toBeNull();
      expect(failure?.recordingReason, entry.operation).toContain(
        'no generator execution proof',
      );
    }
  });

  it('shares one cancellation contract across every live and replay operation', async () => {
    for (const [operation, contract] of Object.entries(modelHubOperationRegistry)) {
      const alreadyAborted = new AbortController();
      const beforeStart = new DOMException(`${operation}:before`, 'AbortError');
      alreadyAborted.abort(beforeStart);
      const neverInvoked = vi.fn(async () => ({}));
      await expect(contract.execute(neverInvoked, {
        signal: alreadyAborted.signal,
      })).rejects.toBe(beforeStart);
      expect(neverInvoked, operation).not.toHaveBeenCalled();

      const controller = new AbortController();
      const commit = vi.fn();
      let settle: ((value: unknown) => void) | undefined;
      const pending = contract.execute(
        () => new Promise((resolve) => {
          settle = resolve;
        }),
        { signal: controller.signal, commit },
      );
      const whilePending = new DOMException(`${operation}:pending`, 'AbortError');
      controller.abort(whilePending);
      await expect(pending).rejects.toBe(whilePending);
      settle?.({});
      await Promise.resolve();
      expect(commit, operation).not.toHaveBeenCalled();
    }

    const corpus = mockCorpusJson as unknown as MockCorpus;
    const observation = corpus.operation_registry.find(
      (entry) => entry.operation === 'observeApiKeySource',
    );
    expect(observation).toBeDefined();
    const controller = new AbortController();
    const reason = new DOMException('dialog closed', 'AbortError');
    controller.abort(reason);
    const draft = observation?.recording.request.body.present
      ? observation.recording.request.body.value
      : null;
    await expect(new MockStore(corpus).observeApiKeySource(
      draft as Parameters<MockStore['observeApiKeySource']>[0],
      controller.signal,
    )).rejects.toBe(reason);
  });
});
