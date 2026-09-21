// A small stateful server for the providers screen, layered over `support.ts`.
//
// `serveModelHub` there answers a scan and an apply, which is all the setup STEP
// needed. This screen reads and WRITES sources, resumes a runtime and reads agent
// supply, and its interesting properties are relations between numbers — the badge
// against what was added, the summary against what exists, the capsule against what
// is left. Canned replies cannot express those: a fixture that always returns the
// same three rows can only be compared against literals copied out of itself.
//
// So this keeps real state and hands the spec a reader for it. Every assertion in
// `providers.spec.ts` compares what the page says to what this server actually holds,
// which is what makes them rules rather than transcriptions.
//
// `support.ts` is L1's file and is imported, never edited.
import type { Page, Route } from '@playwright/test';

/** Shapes only as far as the product reads them; this is a fixture, not a mirror. */
type FakeItem = {
  id: string;
  backend: string;
  kind: string;
  masked_detail: string;
  proposed_action: string;
  selected: boolean;
  vendor?: string;
  display_name?: string;
  masked_credential?: string | null;
};

type FakeSource = {
  id: string;
  vendor: string;
  kind: string;
  display_name: string;
  protocol: string;
  supply_channel: string;
  billing: string;
  state: { status: string };
  models: unknown[];
  masked_credential?: string | null;
  client_nonce?: string | null;
  last_discovered_at: null;
};

/**
 * The scan the screen opens on.
 *
 * Three importable keys across two independent consent groups, plus one native token
 * the entry point must leave alone. The shapes matter to what the screen derives:
 * `openai` and `qwen` rank inside the setup shortlist so they take the two card slots,
 * `zhipu` ranks outside it so it stays an unlisted candidate — which is the only
 * reason the add dialog's 已检测到 method exists at all, and therefore the only way
 * the stable-frame proof can switch between three panes.
 *
 * No row carries `required_backends`, so the two groups are genuinely independent and
 * consenting to one submits one.
 */
export const SCAN_ITEMS: FakeItem[] = [
  {
    id: 'codex-key',
    backend: 'codex',
    kind: 'api_key',
    masked_detail: 'sk-…9d31',
    proposed_action: 'import',
    selected: true,
    vendor: 'openai',
    display_name: 'OpenAI',
    masked_credential: 'sk-…9d31',
  },
  {
    id: 'opencode-qwen',
    backend: 'opencode',
    kind: 'opencode_provider',
    masked_detail: 'sk-…7c08 · 阿里云百炼',
    proposed_action: 'import',
    selected: true,
    vendor: 'qwen',
    display_name: 'alibaba-cn',
    masked_credential: 'sk-…7c08',
  },
  {
    id: 'opencode-zhipu',
    backend: 'opencode',
    kind: 'opencode_provider',
    masked_detail: 'sk-…3456 · 智谱',
    proposed_action: 'import',
    selected: true,
    vendor: 'zhipu',
    display_name: 'zhipu',
    masked_credential: 'sk-…3456',
  },
  {
    id: 'claude-oauth',
    backend: 'claude',
    kind: 'oauth_native',
    masked_detail: 'Claude 订阅令牌',
    proposed_action: 'keep_native',
    selected: false,
  },
];

const CONFIG = {
  platforms: { enabled: [] },
  agents: {
    claude: { enabled: true, cli_path: 'claude' },
    codex: { enabled: true, cli_path: 'codex' },
    opencode: { enabled: true, cli_path: 'opencode' },
  },
  agent: { default_cwd: '/fixture/work' },
  show_duration: false,
  model_hub: { enabled: true },
  capabilities: { model_hub: { enabled: true } },
};

/** Installed, verified and serving: the screen's `gatewayIntent` reads `running` and
 *  starts no install, which is the state every proof below is about something else. */
const RUNTIME = {
  contract_version: 10,
  enabled: true,
  host_platform: 'darwin',
  manifest: {
    name: 'cliproxyapi',
    resolution: 'resolved',
    version: '1.0.0',
    source_sha: 'fixture',
    assets: [],
  },
  status: {
    installed_version: '1.0.0',
    verified: true,
    listening: { host: '127.0.0.1', port: 8317 },
    health: 'ok',
    last_check: null,
  },
};

const AGENTS = ['claude', 'codex', 'opencode'].map((backend) => ({
  backend,
  cli_present: true,
  mode: 'direct',
  menu_kind: 'builtin',
  selected_model_id: null,
  selected_by_agent: null,
}));

/** What the server holds right now — the other half of every assertion. */
export type ServerFacts = {
  /** Every source that exists, however it got there. */
  sources: FakeSource[];
  /** Only the ones written through the add dialog, which is what the badge counts. */
  written: FakeSource[];
  /** Rows a scan would still return. */
  items: FakeItem[];
  /** One entry per apply, holding the exact ids that batch submitted. */
  applied: string[][];
};

export type ProviderServer = {
  facts: () => ServerFacts;
};

const migrated = (item: FakeItem): FakeSource => ({
  id: `src_${item.id}`,
  vendor: item.vendor ?? 'custom',
  kind: 'api_key',
  display_name: item.display_name ?? item.vendor ?? 'custom',
  protocol: 'openai_chat',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active' },
  models: [],
  masked_credential: item.masked_credential ?? null,
  client_nonce: null,
  last_discovered_at: null,
});

/**
 * Answers everything the providers screen reads and writes.
 *
 * Call AFTER `serveProduct`: Playwright matches routes newest-first, which is what
 * lets these override that fixture's `/api/config` and answer the POSTs its catch-all
 * would otherwise refuse. Everything it does not name is still refused, so the fixture
 * cannot reach a running service.
 */
export async function serveProviders(page: Page): Promise<ProviderServer> {
  let items = SCAN_ITEMS.map((item) => ({ ...item }));
  const sources: FakeSource[] = [];
  const written: FakeSource[] = [];
  const applied: string[][] = [];

  // The writes are POSTs, so the product asks for a CSRF token first. The base
  // fixture refuses that along with every other write-enabling request.
  await page.route('**/api/csrf-token', (route) =>
    route.fulfill({ json: { csrf_token: 'fixture-token' } }));
  await page.route('**/api/config', (route) => route.fulfill({ json: CONFIG }));

  await page.route('**/api/models/sources', (route: Route) => {
    if (route.request().method() !== 'POST') return route.fulfill({ json: { sources } });
    const draft = JSON.parse(route.request().postData() || '{}');
    const source: FakeSource = {
      id: `src_written_${written.length + 1}`,
      vendor: draft.vendor ?? 'custom',
      kind: 'api_key',
      display_name: draft.display_name ?? draft.vendor ?? 'custom',
      protocol: draft.protocol ?? 'openai_chat',
      supply_channel: 'hub',
      billing: 'metered',
      state: { status: 'active' },
      models: [],
      // Masked by the server, which is the only side that ever sees the key.
      masked_credential: `${String(draft.key ?? '').slice(0, 3)}…${String(draft.key ?? '').slice(-4)}`,
      client_nonce: draft.client_nonce ?? null,
      last_discovered_at: null,
    };
    sources.push(source);
    written.push(source);
    return route.fulfill({ json: { source, added_to: [], adopted_by: [] } });
  });

  await page.route('**/api/models/migration/scan', (route) =>
    route.fulfill({ json: { scan: { items } } }));
  await page.route('**/api/models/migration/apply', (route) => {
    const ids: string[] = JSON.parse(route.request().postData() || '{}').item_ids ?? [];
    applied.push(ids);
    // A migration produces sources too. Removing the rows it consumed is what makes
    // the capsule's remainder the server's answer rather than the page's subtraction.
    for (const item of items) if (ids.includes(item.id)) sources.push(migrated(item));
    items = items.filter((item) => !ids.includes(item.id));
    return route.fulfill({ json: { applied: ids.length, sources: [] } });
  });

  await page.route('**/api/models/agents**', (route) => route.fulfill({ json: { agents: AGENTS } }));
  await page.route('**/api/models/runtime/status', (route) => route.fulfill({ json: { runtime: RUNTIME } }));
  await page.route('**/api/models/runtime/install', (route) => route.fulfill({ json: { runtime: RUNTIME } }));
  await page.route('**/api/models/runtime/start', (route) => route.fulfill({ json: { runtime: RUNTIME } }));

  return {
    facts: () => ({
      sources: sources.map((source) => ({ ...source })),
      written: written.map((source) => ({ ...source })),
      items: items.map((item) => ({ ...item })),
      applied: applied.map((batch) => [...batch]),
    }),
  };
}

/** Distinct provider identities the server holds — what the summary sentence counts. */
export const vendorCount = (facts: ServerFacts): number =>
  new Set(facts.sources.map((source) => source.vendor)).size;

/** Importable rows a scan would still offer — what the capsule's sentence counts. */
export const importableCount = (facts: ServerFacts): number =>
  facts.items.filter((item) => item.proposed_action === 'import').length;

export async function openProviders(page: Page, options: { lang?: string; theme?: string } = {}) {
  const query = new URLSearchParams({ lang: options.lang ?? 'en', theme: options.theme ?? 'dark' });
  await page.goto(`/e2e/onboarding-fidelity/providers-fixture.html?${query}`);
  await page.locator('.setup-provider-stage').waitFor();
  // The screen's first read is a scan and a source list in one `Promise.all`; the
  // summary is the last thing either of them moves, so it is what "settled" means here.
  await page.locator('.setup-provider-summary').waitFor();
}
