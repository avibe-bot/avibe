import { ApiCallError, modelsApi } from '../../src/components/settings/models/modelsApi';
import type { QuotaSummary, ResetCard, ResetCardLock, ResetCardUseResult, SourceQuota } from '../../src/components/settings/models/types';
import { installShellFixture } from './shellApi';

// The complete Models page on its 订阅额度 tab, with every vendor answer stubbed at the API
// boundary: `modelsApi.getQuota`, `refreshQuota` and `redeemResetCard` are replaced in memory
// (the shell fixture replaces the other reads). Nothing reaches Avibe or any vendor; a use
// is answered here by the state the spec names.
const MINUTE = 60_000;
const at = (minutes: number) => new Date(Date.now() + minutes * MINUTE).toISOString();
const day = (days: number) => {
  const date = new Date(Date.now() + days * 24 * 60 * MINUTE);
  date.setUTCHours(9, 0, 0, 0);
  return date.toISOString();
};

const card = (over: Partial<ResetCard> & Pick<ResetCard, 'kind'>): ResetCard => ({
  count: 1, expires_at: null, stated: true, usable: true, blocked: null, blocked_until: null, ...over,
});
const full = card({ kind: 'all', expires_at: day(22) });
const fable = card({ kind: 'fable', count: 3, expires_at: day(8) });
const chatgpt = card({ kind: 'account', count: 2, expires_at: day(8), stated: false });

type Held = { claude: ResetCard[]; chatgpt: ResetCard[]; lock?: ResetCardLock | null };

const summary = ({ claude, chatgpt: gpt, lock = null }: Held, reset = false): QuotaSummary => {
  const window = (id: string, kind: 'session' | 'weekly' | 'model_weekly', used: number, resetsIn: number, scope?: string) => ({
    id, kind, label: scope ?? id, ...(scope ? { scope_model: scope } : {}), used_pct: used,
    window_seconds: kind === 'session' ? 18_000 : 604_800, resets_at: at(resetsIn),
  });
  const sources: SourceQuota[] = [
    {
      source_id: 'src_claude', vendor: 'anthropic', display_name: 'Claude Max', account_label: 'alex@example.com', plan: 'max',
      fetched_at: at(reset ? 0 : -2), state: 'ok',
      windows: [
        window('five_hour', 'session', reset ? 0 : 100, reset ? 300 : 130),
        window('seven_day', 'weekly', reset ? 22 : 41, 4 * 24 * 60 + 300),
        // Fable's own weekly limit, by the id the contract's `cleared` names.
        window('seven_day_overage_included', 'model_weekly', reset ? 0 : 88, 4 * 24 * 60 + 300, 'Fable'),
      ],
      reset_cards: { state: 'ok', lock, cards: claude },
    },
    {
      source_id: 'src_chatgpt', vendor: 'openai', display_name: 'ChatGPT Pro', account_label: 'alex@example.com', plan: 'pro',
      fetched_at: at(-1), state: 'ok',
      windows: [window('primary_window', 'session', 22, 47), window('secondary_window', 'weekly', 58, 4 * 24 * 60 + 300)],
      reset_cards: { state: 'ok', lock: null, cards: gpt },
    },
    // A long account name: at phone width the status goes under it, so the refresh never truncates it.
    {
      source_id: 'src_claude_team', vendor: 'anthropic', display_name: 'Claude Max · Design team', account_label: 'design@example.com', plan: 'max',
      fetched_at: at(-3), state: 'ok',
      windows: [window('five_hour', 'session', 64, 130), window('seven_day', 'weekly', 30, 4 * 24 * 60 + 300)],
      reset_cards: { state: 'ok', lock: null, cards: [] },
    },
  ];
  return { refresh_interval_seconds: 300, sources };
};

const both: Held = { claude: [full, fable], chatgpt: [chatgpt] };
const STATES: Record<string, Held> = {
  none: { claude: [], chatgpt: [] },
  'one-kind': { claude: [fable], chatgpt: [chatgpt] },
  'not-usable': {
    claude: [full, { ...fable, usable: false, blocked: 'needs_limit' }],
    chatgpt: [{ ...chatgpt, usable: false, blocked: 'cooldown', blocked_until: at(130) }],
  },
};

const never = <T,>() => new Promise<T>(() => {});

export function installResetCardsFixture(state: string) {
  installShellFixture();
  modelsApi.getHostedTools = async () => ({ tools: [] });
  const held = STATES[state] ?? both;
  // A card that waits for a limit is drawn, as in the frame, while no limit is reached.
  const reset = state === 'not-usable';
  modelsApi.getQuota = async () => summary(held, reset);
  modelsApi.refreshQuota = state === 'refreshing' ? never : async () => summary(held, reset);
  modelsApi.redeemResetCard = async (_sourceId, kind): Promise<ResetCardUseResult> => {
    if (state === 'using') return never();
    if (state === 'used') {
      return {
        quota: summary({ ...both, claude: [full, { ...fable, count: 2 }] }, true),
        use: { kind, outcome: 'used', reason: null, cleared: ['five_hour', 'seven_day_overage_included'] },
      };
    }
    if (state === 'not-used') return { quota: summary(both), use: { kind, outcome: 'not_used', reason: 'cooldown', cleared: [] } };
    if (state === 'unconfirmed') {
      return {
        quota: summary({ ...both, lock: { reason: 'unconfirmed', kind, since: at(0) } }),
        use: { kind, outcome: 'unconfirmed', reason: null, cleared: [] },
      };
    }
    throw new ApiCallError('reset_card_unavailable', undefined, true, [], [], [], 409);
  };
}
