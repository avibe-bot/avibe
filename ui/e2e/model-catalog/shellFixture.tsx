import { MemoryRouter } from 'react-router-dom';
import { SettingsModelsPage } from '../../src/components/settings/models/SettingsModelsPage';
import { modelsApi } from '../../src/components/settings/models/modelsApi';
import { CONTRACT_VERSION, type AgentSupply, type Source } from '../../src/components/settings/models/types';
import { ToastProvider } from '../../src/context/ToastProvider';

// Only reads at the API boundary are replaced. Layout and interactions render
// the complete shipped Models page, with no account or credential data.
function installShellFixture() {
  const backends = ['claude', 'codex', 'opencode', 'vibey'] as const;
  const sources: Source[] = ['Anthropic API', 'OpenAI API', 'Grok API', 'OpenAI'].map((name, index) => ({
    id: `src_shell${index}`, display_name: name, vendor: index === 0 ? 'anthropic' : 'openai',
    kind: index === 3 ? 'subscription' : 'api_key',
    protocol: index === 0 ? 'anthropic' : 'openai_responses', supply_channel: 'hub',
    billing: index === 3 ? 'monthly' : 'metered', last_discovered_at: null,
    base_url: index === 3 ? null : 'https://api.example.com/v1',
    account_label: index === 3 ? 'demo@example.com' : null,
    state: { status: 'active', retry_at: null, detail_key: null }, models: [],
    adopted_by: backends.map((backend) => ({ backend, menu_model: 'example-model' })),
  }));
  const agents: AgentSupply[] = backends.map((backend) => ({
    backend, cli_present: backend !== 'vibey', mode: 'hub', menu_kind: 'open', catalog_models: [],
    sources: { order: sources.map((source) => source.id), eligibility: sources.map((source) => ({ source_id: source.id, eligible: true })) },
  }));
  modelsApi.listSources = async () => sources;
  modelsApi.listAgents = async () => agents;
  modelsApi.refreshAgentPresence = async () => agents;
  modelsApi.getAgentProvenance = async () => null;
  modelsApi.getAgentChains = async () => [];
  modelsApi.listEvents = async () => [];
  modelsApi.getRuntimeStatus = async () => ({
    contract_version: CONTRACT_VERSION, enabled: true,
    manifest: { name: 'cliproxyapi', resolution: 'resolved', version: '1.0.0', source_sha: 'fixture', assets: [] },
    status: { installed_version: '1.0.0', verified: true, health: 'ok' },
  });
  modelsApi.scanMigration = async () => ({
    contract_version: CONTRACT_VERSION,
    items: [{ id: 'mig_shell', backend: 'claude', kind: 'oauth_native', masked_detail: 'Demo account', proposed_action: 'import', selected: true, notes_key: null }],
  });
}

if (new URLSearchParams(location.search).get('view') === 'shell') installShellFixture();

export function ShellFixture() {
  return <MemoryRouter><ToastProvider>
    <main className="mx-auto min-w-0 max-w-[1100px] p-4"><SettingsModelsPage /></main>
  </ToastProvider></MemoryRouter>;
}
