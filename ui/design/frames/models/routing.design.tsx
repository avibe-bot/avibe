// Routing a backend's models: 默认路由 (the shipped SourceOrderDrawer, the order every automatic
// route follows) and one model's route (the shipped RouteChainDialog, opened from a model row).
import { createSourceCollectionReadAuthority } from '@/components/settings/models/collectionReadAuthority';
import { readyRegion } from '@/components/settings/models/regionRead';
import { RouteChainDialog } from '@/components/settings/models/RouteChainDialog';
import { SourceOrderDrawer } from '@/components/settings/models/SourceOrderDrawer';
import type { AgentBackend, AgentSupply, Source, TurnProvenance } from '@/components/settings/models/types';

import { frames } from '../../frame';
import { AfterMount } from '../../shell/AfterMount';
import { agent, agentOf, at, chainOf, claudeMax, demoAgents, demoSources, hubApi, ok, relayClaude, type AgentSpec } from './fixtures';

const order = (backend: AgentBackend) => {
  const supply = agentOf(backend);
  const sourceReads = createSourceCollectionReadAuthority();
  const orderWrite = { pending: false, track: () => new Promise<void>(() => {}) };
  return () => (
    <SourceOrderDrawer open agent={supply} sources={demoSources} sourceReads={sourceReads} onClose={() => {}} onSaved={() => {}} orderWrite={orderWrite} />
  );
};

/** One model's route dialog, opened after mount as the page does. The selection is built once: the dialog re-reads the route whenever it changes. */
const route = (backend: AgentBackend, modelId: string, specs: AgentSpec[] = demoAgents, sources: Source[] = demoSources) => {
  const supply: AgentSupply = agent(specs.find((spec) => spec.backend === backend)!);
  const selection = { agent: supply, modelId, read: readyRegion(chainOf(backend, modelId, specs)) };
  const readAgents = async () => ({ value: [supply], install: () => {} });
  const readSources = async () => ({ value: sources, install: () => {} });
  return () => (
    <AfterMount>
      {(open) => (
        <RouteChainDialog
          selection={open ? selection : null}
          sources={sources}
          onClose={() => {}}
          readAgents={readAgents}
          readSources={readSources}
          onOpenDefaults={() => {}}
          onManageModel={() => {}}
        />
      )}
    </AfterMount>
  );
};

// The last turn on claude-opus-5 ended in an upstream error on its second hop.
const upstreamError: TurnProvenance = {
  contract_version: 23,
  turn_id: 'turn_design',
  ts: at(-6),
  agent: 'claude',
  requested_model_id: 'claude-opus-5',
  outcome: 'failed_terminal',
  failed_attempts: [],
  served: null,
  canceled_attempt: null,
  terminal_error: {
    source_id: relayClaude.id,
    configured_model_id: 'claude-opus-5',
    channel: 'hub',
    reason: 'invalid_parameter',
    stream_started: false,
    http_status: 400,
    upstream_error_code: 'invalid_request_error',
    upstream_detail: 'max_tokens: 200000 > 128000, which is the maximum allowed number of output tokens for claude-opus-5',
    response_correlation_ids: [{ header: 'request-id', value: 'req_011CUf3kR8ZqBvN7xDesign' }],
  },
  model_supply_state: null,
  blockers: [],
};
const relayFirstAgents: AgentSpec[] = demoAgents.map((spec) => spec.backend !== 'claude' ? spec : {
  ...spec,
  routes: { ...spec.routes, 'claude-opus-5': { hops: [[relayClaude, 'claude-opus-5'], [claudeMax, 'claude-opus-5']], origin: 'manual' } },
});

// A model added to the menu that no provider serves yet.
const unroutedAgents: AgentSpec[] = demoAgents.map((spec) => spec.backend !== 'opencode' ? spec : {
  ...spec,
  routes: { ...spec.routes, 'kimi-k3': { hops: [] } },
});

const api = hubApi({ agents: demoAgents });

const designFrames = frames([
  {
    name: 'default-order',
    title: '默认路由 · Codex（供应商顺序）',
    width: 1280,
    height: 800,
    status: 'shipped',
    fixtures: { api },
    render: order('codex'),
  },
  {
    name: 'default-order-phone',
    title: '默认路由 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures: { api },
    render: order('opencode'),
  },
  {
    name: 'route-automatic',
    title: '模型路由 · 自动（按默认路由）',
    width: 1000,
    height: 760,
    status: 'shipped',
    fixtures: { api },
    render: route('codex', 'gpt-5.6-sol'),
  },
  {
    name: 'route-manual',
    title: '模型路由 · 手动',
    width: 1000,
    height: 760,
    status: 'shipped',
    fixtures: { api },
    render: route('codex', 'gpt-5.6-terra'),
  },
  {
    name: 'route-passthrough',
    title: '模型路由 · 透传',
    width: 1000,
    height: 760,
    status: 'shipped',
    fixtures: { api },
    render: route('codex', 'gpt-5.6-luna'),
  },
  {
    name: 'route-upstream-error',
    title: '模型路由 · 最近一轮上游报错',
    width: 1000,
    height: 760,
    status: 'shipped',
    fixtures: {
      api: { ...hubApi({ agents: relayFirstAgents }), '/api/models/agents/claude/provenance?model=claude-opus-5': ok({ provenance: upstreamError }) },
    },
    render: route('claude', 'claude-opus-5', relayFirstAgents),
  },
  {
    name: 'route-empty',
    title: '模型路由 · 还没有供应商提供',
    width: 1000,
    height: 760,
    status: 'shipped',
    fixtures: { api: hubApi({ agents: unroutedAgents }) },
    render: route('opencode', 'kimi-k3', unroutedAgents),
  },
  {
    name: 'route-phone',
    title: '模型路由 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures: { api },
    render: route('claude', 'claude-opus-5'),
  },
]);

export default designFrames;
