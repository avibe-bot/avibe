// 订阅额度 · 重置卡 (approved by the owner 2026-11-20, 「按这稿做」; shipped): the quota tab, with each
// account's reset cards inside its card and a refresh on the card. One frame per state the owner
// asked for: none, one kind, both kinds, the line before a use, using, used, not used, an answer
// that never arrived, and a card the vendor does not let through yet. What the server read (cards,
// lock) comes from the summary; the page's own step for an account comes from `resetCardPhases`.
import { QuotaTab } from '@/components/settings/models/QuotaTab';
import type { ResetCardPhase } from '@/components/settings/models/ResetCards';
import { readyRegion } from '@/components/settings/models/regionRead';
import type { QuotaSummary, ResetCard, ResetCardLock, SourceQuota } from '@/components/settings/models/types';

import { frames, type FrameSpec } from '../../frame';
import { DESIGN_NOW } from '../../shell/clock';
import { at, baseApi, chatgptPro, claudeMax, quotaSummary, settingsPaneClass } from './fixtures';

const nov28 = '2026-11-28T09:00:00Z';
const dec12 = '2026-12-12T09:00:00Z';
const card = (over: Partial<ResetCard> & Pick<ResetCard, 'kind'>): ResetCard => ({
  count: 1, expires_at: null, stated: true, usable: true, blocked: null, blocked_until: null, ...over,
});

const full = card({ kind: 'all', expires_at: dec12 });
const fable = card({ kind: 'fable', count: 3, expires_at: nov28 });
// ChatGPT states nothing about what its card resets.
const chatgpt = card({ kind: 'account', count: 2, expires_at: nov28, stated: false });

type Held = {
  claude?: ResetCard[];
  chatgpt?: ResetCard[];
  lock?: ResetCardLock;
  phase?: ResetCardPhase;
  refreshing?: boolean;
};

// Claude Max has used up its 5-hour limit: the moment a reset card is for. After a use the same
// card shows the reading the server returned with its answer.
const summary = ({ claude = [], chatgpt: gpt = [], lock }: Held, reset: boolean): QuotaSummary => {
  const base = quotaSummary();
  return {
    ...base,
    sources: base.sources.map((source): SourceQuota => source.source_id === chatgptPro.id
      ? { ...source, reset_cards: { state: 'ok', lock: null, cards: gpt } }
      : source.source_id !== claudeMax.id ? source : {
        ...source,
        fetched_at: at(reset ? 0 : -2),
        reset_cards: { state: 'ok', lock: lock ?? null, cards: claude },
        windows: source.windows.map((window) => window.id === 'five_hour'
          ? { ...window, used_pct: reset ? 0 : 100, resets_at: at(reset ? 300 : 130) }
          // Fable's own weekly limit, the one a Fable card gives back.
          : window.id === 'seven_day_opus'
            ? { ...window, id: 'seven_day_overage_included', kind: 'model_weekly' as const, label: 'Fable', scope_model: 'Fable', used_pct: reset ? 0 : 88 }
            : window.id === 'seven_day' && reset ? { ...window, used_pct: 22 } : window),
      }),
  };
};

const quota = (held: Held, reset = false) => {
  const read = readyRegion(summary(held, reset));
  return () => (
    <div className={settingsPaneClass}>
      <QuotaTab
        quota={read}
        now={DESIGN_NOW}
        refreshing={held.refreshing}
        refreshingSourceId={held.refreshing ? claudeMax.id : null}
        resetCardPhases={held.phase ? { [claudeMax.id]: held.phase } : {}}
      />
    </div>
  );
};

const desktop = (name: string, title: string, held: Held, reset = false): FrameSpec => ({
  name, title: `订阅额度 · 重置卡 · ${title}`, width: 1280, height: 1080, status: 'shipped', fixtures: { api: baseApi }, render: quota(held, reset),
});
const phone = (name: string, title: string, held: Held, reset = false): FrameSpec => ({
  name: `${name}-phone`, title: `订阅额度 · 重置卡 · ${title} · 手机`, width: 390, height: 2000, status: 'shipped', fixtures: { api: baseApi }, render: quota(held, reset),
});

const both = [full, fable];
const afterUse = [full, { ...fable, count: 2 }];
const confirm: ResetCardPhase = { phase: 'confirm', kind: 'fable', stated: true };
const unconfirmed: ResetCardLock = { reason: 'unconfirmed', kind: 'fable', since: at(-1) };

const designFrames = frames([
  desktop('none', '没有重置卡', {}),
  desktop('refreshing', '刷新这个账号', { claude: both, chatgpt: [chatgpt], refreshing: true }),
  desktop('one-kind', '一种', { claude: [fable], chatgpt: [chatgpt] }),
  desktop('both-kinds', '两种', { claude: both, chatgpt: [chatgpt] }),
  desktop('confirm', '使用前', { claude: both, phase: confirm }),
  // What every open page shows while the server holds the use.
  desktop('using', '使用中', { claude: both, lock: { reason: 'in_flight', kind: 'fable', since: at(0) } }),
  desktop('used', '已使用', { claude: afterUse, phase: { phase: 'used', kind: 'fable', stated: true } }, true),
  desktop('failed', '没有用掉', { claude: both, phase: { phase: 'not_used', kind: 'fable', reason: 'cooldown' } }),
  desktop('unconfirmed', '结果未确认', { claude: both, lock: unconfirmed }),
  desktop('not-usable', '现在不能用', {
    claude: [full, { ...fable, usable: false, blocked: 'needs_limit' }],
    chatgpt: [{ ...chatgpt, usable: false, blocked: 'cooldown', blocked_until: at(130) }],
  }, true),
  phone('both-kinds', '两种', { claude: both, chatgpt: [chatgpt] }),
  phone('confirm', '使用前', { claude: both, phase: confirm }),
  phone('used', '已使用', { claude: afterUse, phase: { phase: 'used', kind: 'fable', stated: true } }, true),
  phone('unconfirmed', '结果未确认', { claude: both, lock: unconfirmed }),
]);

export default designFrames;
