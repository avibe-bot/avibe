// Settings › Experimental › Source filter: the switch, the hook folder and each script's state
// (`docs/plans/vibey-source-filter-hooks.md`). Owner-only: the scripts run on this machine.
import { apiFetch } from '@/lib/apiFetch';

export const SOURCE_FILTER_HOOKS = ['before_search', 'after_search', 'before_fetch', 'after_fetch'] as const;
export type SourceFilterHookName = (typeof SOURCE_FILTER_HOOKS)[number];

export type SourceFilterHook = {
  found: boolean;
  /** The last run that failed and blocked its call, until the script next runs cleanly. ISO time. */
  last_failure: { at: string; reason: string } | null;
};

export type SourceFilterStatus = {
  enabled: boolean;
  dir: string;
  hooks: Record<SourceFilterHookName, SourceFilterHook>;
};

const PATH = '/api/settings/experimental/source-filter';

async function read(response: Response): Promise<SourceFilterStatus> {
  const payload = (await response.json().catch(() => null)) as (SourceFilterStatus & { ok?: boolean; error?: string }) | null;
  if (!response.ok || !payload || payload.ok === false) {
    throw new Error(payload?.error ?? `http_${response.status}`);
  }
  return { enabled: payload.enabled, dir: payload.dir, hooks: payload.hooks };
}

export const sourceFilterApi = {
  get: async (): Promise<SourceFilterStatus> => read(await apiFetch(PATH)),
  setEnabled: async (enabled: boolean): Promise<SourceFilterStatus> => read(await apiFetch(PATH, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  })),
};

/** The public guide: the protocol, the files and examples. */
export const sourceFilterGuideUrl = (language: string): string => (
  language.startsWith('zh')
    ? 'https://docs.avibe.bot/zh/concepts/source-filter'
    : 'https://docs.avibe.bot/concepts/source-filter'
);
