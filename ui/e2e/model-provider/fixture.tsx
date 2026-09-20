import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import en from '../../src/i18n/en.json';
import zh from '../../src/i18n/zh.json';
import '../../src/index.css';
import '../../src/components/settings/models/modelHubSurface.css';
import { Button } from '../../src/components/ui/button';
import { AddApiKeyDialog } from '../../src/components/settings/models/AddApiKeyDialog';
import { SourceDetailPanel } from '../../src/components/settings/models/SourceDetailPanel';
import { createSourceCollectionReadAuthority } from '../../src/components/settings/models/collectionReadAuthority';
import { modelsApi } from '../../src/components/settings/models/modelsApi';
import { readSurfaceLanding, sourceMutationLanding, type TrackSourceMutation } from '../../src/components/settings/models/mutationSettlement';
import { CONTRACT_VERSION, type Source } from '../../src/components/settings/models/types';

const params = new URLSearchParams(location.search);
const language = createInstance();
await language.init({ lng: params.get('lang') ?? 'en', resources: { en: { translation: en }, zh: { translation: zh } }, interpolation: { escapeValue: false } });
document.documentElement.dataset.theme = params.get('theme') ?? 'dark';
let saved: Source = {
  id: 'src_fixture001', vendor: 'custom', kind: 'api_key', display_name: 'Example relay',
  base_url: 'https://relay.example/v1', protocol: 'openai_chat', billing: 'metered', supply_channel: 'hub',
  state: { status: 'standby', retry_at: null, detail_key: null }, credential_ref: 'cred_fixture001',
  verification_pending: 'vp_fixture', last_discovered_at: '2026-09-08T00:00:00Z',
  models: ['upstream-first-model', 'gpt-5.6-luna', 'claude-sonnet-5'].map((id) => ({
    id, display_name: null, origin: 'discovered', reasoning_efforts: ['low', 'medium', 'high'], reasoning_efforts_source: 'catalog',
  })),
};
const calls: string[] = [];
modelsApi.listSources = async () => [saved];
modelsApi.observeApiKeySource = async () => { throw new Error('Automatic detection must not run'); };
modelsApi.createApiKeySource = async (draft) => {
  calls.push('save');
  if (!draft.save_unverified) throw new Error('Save must not require detection');
  saved = { ...saved, client_nonce: draft.client_nonce, protocol: draft.protocol ?? saved.protocol };
  return { source: saved, added_to: [], adopted_by: [] };
};
modelsApi.probeSource = async (id, model) => {
  calls.push('test:' + id + '/' + model);
  saved = { ...saved, verification_pending: null };
  return { source_id: id, model_id: model, protocol: saved.protocol, reachable: true, latency_ms: 123, error: null };
};
modelsApi.refreshSource = async () => { calls.push('refetch'); return { source: saved, discovered: saved.models.length }; };
modelsApi.addCustomModel = async (_id, draft) => {
  calls.push('manual');
  saved = { ...saved, models: [...saved.models, {
    id: draft.model_id, display_name: null, origin: 'manual', reasoning_efforts: [], reasoning_efforts_source: null,
  }] };
  return saved;
};
const sourceReads = createSourceCollectionReadAuthority(modelsApi);

export function Fixture() {
  const [open, setOpen] = useState(false);
  const [source, setSource] = useState(saved);
  const [callLog, setCallLog] = useState<string[]>([]);
  const reconcile = async () => {
    const reads = await readSurfaceLanding({
      sources: async () => {
        if (params.get('reconcile') === 'failed') throw new Error('Fixture Source read unavailable');
        return modelsApi.listSources();
      },
      supply: async () => [],
      runtime: async () => ({
        contract_version: CONTRACT_VERSION,
        manifest: { name: 'cliproxyapi', resolution: 'unresolved', assets: [] },
        status: { verified: false, health: 'not_started' },
      }),
      chains: async () => ({}),
    }, []);
    if (reads.sources.kind === 'ready') setSource(saved);
    return sourceMutationLanding(reads, [], true);
  };
  const trackMutation: TrackSourceMutation = async (work) => {
    try {
      return await work(saved, {
        source: async (next) => { setSource(next); return reconcile(); },
        unread: reconcile,
        gone: async () => ({ verdict: 'degraded', reads: null, affectedChains: [] }),
        readInventory: async () => ({ sources: [saved], snapshot: 1 }),
        release: () => {},
      });
    } finally {
      setCallLog([...calls]);
    }
  };
  return <I18nextProvider i18n={language}>
    <main className="mx-auto flex h-dvh w-full max-w-[720px] flex-col bg-surface">
      <Button onClick={() => setOpen(true)}>Add provider fixture</Button>
      <SourceDetailPanel source={source} trackMutation={trackMutation} onReauth={() => {}}
        onMutationCommitted={async (commit) => { await commit.settle(); }} />
      <AddApiKeyDialog open={open} sourceReads={sourceReads} onClose={() => setOpen(false)}
        onAdded={(created) => setSource(created.source)} />
      <output hidden data-testid="calls">{JSON.stringify(callLog)}</output>
    </main>
  </I18nextProvider>;
}
createRoot(document.getElementById('root')!).render(<Fixture />);
