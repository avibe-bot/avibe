import { useState } from 'react';
import { RouteChainDialog } from '../../src/components/settings/models/RouteChainDialog';
import { modelsApi } from '../../src/components/settings/models/modelsApi';
import { readyRegion } from '../../src/components/settings/models/regionRead';
import type { AgentBackend, AgentChain, AgentSupply, RouteHop, Source } from '../../src/components/settings/models/types';

const params = new URLSearchParams(location.search);
const backend = (params.get('backend') ?? 'codex') as AgentBackend;
const origin = (params.get('origin') ?? 'automatic') as AgentChain['route_origin'];
const modelId = params.has('long') ? `模型/${'long-model-identity/'.repeat(10)}gpt-test` : 'gpt-test';
const sources: Source[] = ['a', 'b'].map((id) => ({
  id: `src_${id}`, display_name: id === 'a' ? 'Provider A' : 'Provider B',
  kind: 'api_key', vendor: 'openai', protocol: 'openai_responses',
  supply_channel: 'hub', billing: 'metered', last_discovered_at: null,
  state: { status: 'active', retry_at: null, detail_key: null },
  models: [{ id: modelId, origin: 'discovered', reasoning_efforts: [], reasoning_efforts_source: null }],
}));
const inheritedHops = sources.map((source) => ({ source_id: source.id, model_id: modelId }));
const supply: AgentSupply = {
  backend, mode: 'hub', cli_present: true, menu_kind: 'fixed',
  sources: { order: sources.map((source) => source.id), eligibility: sources.map((source) => ({ source_id: source.id, eligible: true })) },
};
const makeChain = (hops: RouteHop[], manual: boolean): AgentChain => ({
  contract_version: 10, backend, model_id: modelId,
  manual_override: manual ? { hops } : null,
  route_origin: manual ? 'manual' : origin === 'passthrough' ? 'passthrough' : 'automatic',
  current: hops[0] ?? null, supply_state: hops.length ? 'ok' : 'interrupted',
  chain: hops.map((hop) => ({ ...hop, channel: 'hub', health: 'healthy', runnable: true, reason: null, retry_at: null })),
});
let saved = makeChain(inheritedHops, origin === 'manual');
let reads = 0;
const writes: Array<{ method: string; hops?: RouteHop[] }> = [];
// This fixture calls production components and replaces only their API boundary.
// It must not connect to the personal service or an Incus environment.
if (params.get('view') === 'route') {
  modelsApi.getAgentChain = async () => { reads += 1; return saved; };
  modelsApi.getAgentProvenance = async () => null;
  modelsApi.previewAgentChain = async () => makeChain(inheritedHops, false);
  modelsApi.putAgentChain = async (target, model, input) => {
    if (target !== backend || model !== modelId) throw new Error('Wrong route identity');
    writes.push({ method: 'PUT', hops: input.hops });
    saved = makeChain(input.hops, true);
    return { chain: saved, removed_hops: [], interrupted: [] };
  };
  modelsApi.restoreAgentChain = async (target, model) => {
    if (target !== backend || model !== modelId) throw new Error('Wrong route identity');
    writes.push({ method: 'DELETE' });
    saved = makeChain(inheritedHops, false);
    return { chain: saved, removed_hops: [], interrupted: [] };
  };
}

export function RouteFixture() {
  const [open, setOpen] = useState(false);
  const [, setRevision] = useState(0);
  const observed = () => setRevision((revision) => revision + 1);
  return <>
    <button type="button" onClick={() => setOpen(true)}>Open route</button>
    <output data-testid="route-state">{JSON.stringify({ saved, reads, writes })}</output>
    <RouteChainDialog selection={open ? { agent: supply, modelId, read: readyRegion(saved) } : null}
      sources={sources} onClose={() => setOpen(false)} onObserved={observed} onCommitted={observed}
      readAgents={async () => ({ value: [supply], install: observed })}
      readSources={async () => ({ value: sources, install: observed })} />
  </>;
}
