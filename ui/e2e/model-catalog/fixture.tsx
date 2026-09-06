import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import en from '../../src/i18n/en.json';
import zh from '../../src/i18n/zh.json';
import '../../src/index.css';
import '../../src/components/settings/models/modelHubSurface.css';
import { BackendModelCatalogDialog } from '../../src/components/settings/models/BackendModelCatalogDialog';
import { blankBackendModel } from '../../src/components/settings/models/backendCatalog';
import { modelsApi } from '../../src/components/settings/models/modelsApi';
import type { AgentBackend, AgentSupply, BackendModel, BackendModelsPut } from '../../src/components/settings/models/types';

const params = new URLSearchParams(location.search);
const backend = params.get('backend') as AgentBackend;
const language = createInstance();
await language.init({ lng: params.get('lang') ?? 'en', resources: { en: { translation: en }, zh: { translation: zh } }, interpolation: { escapeValue: false } });
let saved: BackendModel[] = [{ ...blankBackendModel(), id: 'claude-existing-model', display_name: 'Existing model', context_window: 200000 }];
const writes: BackendModelsPut[] = [];
const supply = (): AgentSupply => ({
  backend, cli_present: true, mode: 'hub', menu_kind: backend === 'opencode' ? 'open' : 'fixed',
  sources: { order: [], eligibility: [] }, routes: {}, builtin_models: [], menu: null, catalog_models: saved,
});

// Only the API boundary is replaced; Radix portals, focus, touch events and
// catalog draft/commit consumers are the shipped components.
modelsApi.getAgentSources = async () => supply();
modelsApi.getAgentModelCandidates = async () => ({
  builtin: [], in_list: [],
  providers: ['claude-candidate-alpha', 'claude-candidate-beta'].map((id) => ({
    id, display_name: null, reasoning_efforts: [], suppliers: [], origin: 'provider' as const,
    ...(backend === 'opencode' ? { native_protocol: 'anthropic' as const } : {}),
  })),
});
modelsApi.searchModelsDev = async () => [];
modelsApi.putAgentModels = async (target, input) => {
  if (target !== backend) throw new Error('Wrong backend');
  writes.push(input);
  saved = input.models;
  return supply();
};

export function Fixture() {
  const [open, setOpen] = useState(false);
  const [, setRevision] = useState(0);
  return <I18nextProvider i18n={language}>
    <button type="button" onClick={() => setOpen(true)}>Manage models</button>
    <output data-testid="saved">{JSON.stringify({ saved, writes })}</output>
    {open && <BackendModelCatalogDialog open backend={backend} canReadSources sourceNames={{}}
      onClose={() => setOpen(false)} onSaved={() => setRevision((value) => value + 1)}
      onObserved={() => {}} catalogWrite={{ pending: false, track: async (work) => work() }} />}
  </I18nextProvider>;
}

createRoot(document.getElementById('root')!).render(<Fixture />);
