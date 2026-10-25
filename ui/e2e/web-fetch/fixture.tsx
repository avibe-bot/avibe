import * as React from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import en from '../../src/i18n/en.json';
import zh from '../../src/i18n/zh.json';
import '../../src/index.css';
import '../../src/components/settings/models/modelHubSurface.css';
import { ToolsTab } from '../../src/components/settings/models/ToolsTab';
import { HostedToolRow } from '../../src/components/workbench/HostedToolRow';
import { AddSearchServiceDialog } from '../../src/components/settings/models/AddSearchServiceDialog';
import { createSourceCollectionReadAuthority } from '../../src/components/settings/models/collectionReadAuthority';
import { modelsApi } from '../../src/components/settings/models/modelsApi';
import { readyRegion } from '../../src/components/settings/models/regionRead';
import { KEYLESS_ACTIVE_SEARCH, SEARCH_SOURCES } from '../../src/components/settings/models/searchServices.fixtures';
import fetchRead from '../../src/components/settings/models/webFetch.fixture.json';
import fixtures from '../../src/lib/hostedFetchActivity.fixture.json';
import { hostedActivityFrom } from '../../src/lib/hostedActivity';
import type { HostedWebFetchToolStatus } from '../../src/components/settings/models/types';

const params = new URLSearchParams(location.search);
const language = createInstance();
await language.init({ lng: params.get('lang') ?? 'zh', resources: { en: { translation: en }, zh: { translation: zh } }, interpolation: { escapeValue: false } });
document.documentElement.dataset.theme = 'dark';
const fetchTool = fetchRead as unknown as HostedWebFetchToolStatus;
const sourceReads = createSourceCollectionReadAuthority(modelsApi);
const names = ['local_truncated', 'jina_fallback', 'tavily', 'exa', 'challenge', 'policy'] as const;
const noop = () => {};
export function Fixture() {
  const [dialog, setDialog] = React.useState(false);
  const [writes, setWrites] = React.useState<unknown[]>([]);
  return <main className="model-hub-shell mx-auto max-w-[1280px] p-6" data-fixture>
    {params.get('view') === 'chat' ? <>
      <h1 className="mb-5 text-lg font-bold">Web · 网页读取</h1>
      <div className="flex flex-col gap-3">{names.map((name) => <div key={name} className="rounded-xl border border-border bg-surface p-2"><HostedToolRow card={hostedActivityFrom(fixtures[name].activity)!} /></div>)}</div>
      <h2 className="mb-4 mt-9 font-bold">IM · 对应的工具消息</h2>
      <div className="flex flex-col gap-5 rounded-xl border border-border bg-surface p-5">{names.map((name) => <div key={name} data-im={name}><p className="mb-1 text-sm font-semibold">Vibey <span className="text-xs font-normal text-muted">APP · 14:03</span></p><p className={`text-xs leading-relaxed ${name === 'policy' ? 'text-destructive-ink' : 'text-muted'}`}>{fixtures[name].zh}</p></div>)}</div>
    </> : <>
      <h1 className="mb-6 text-xl font-bold">模型网关</h1><nav className="mb-5 flex gap-6 border-b border-border pb-3 text-sm">{['供应商与路由', '工具', '订阅额度', '用量', '日志'].map((label) => <span key={label}>{label}</span>)}</nav>
      <ToolsTab read={readyRegion({ tools: [KEYLESS_ACTIVE_SEARCH, ...(params.has('off') ? [] : [fetchTool])] })} sources={SEARCH_SOURCES.map((s) => s.vendor === 'tavily' ? { ...s, id: 'src_tavily01', state: { status: 'active' } } : s)} pending={new Set()} onSave={(tool, body) => setWrites((old) => [...old, { tool, body }])} onClear={noop} onRetry={noop} onAddService={() => setDialog(true)} onOpenService={noop} onRetryFetch={noop} />
      <output hidden data-writes>{JSON.stringify(writes)}</output>
      {dialog && <AddSearchServiceDialog tool={KEYLESS_ACTIVE_SEARCH} fetchTool={params.has('off') ? undefined : fetchTool} sourceReads={sourceReads} pending={false} onClose={() => setDialog(false)} onAdded={noop} onOpenSource={noop} onViewOrder={() => setDialog(false)} />}
    </>}
  </main>;
}
createRoot(document.getElementById('root')!).render(<I18nextProvider i18n={language}><Fixture /></I18nextProvider>);
