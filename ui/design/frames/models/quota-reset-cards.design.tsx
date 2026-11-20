// 订阅额度 · 重置卡 (proposal): the shipped quota tab, with each account's reset cards drawn inside
// its card and a refresh on the card. One frame per state the owner asked for: none, one kind,
// both kinds, the line before a use, using, used, not used, an answer that never arrived, and a
// card the vendor does not let through yet.
import { QuotaTab } from '@/components/settings/models/QuotaTab';
import { readyRegion } from '@/components/settings/models/regionRead';
import type { QuotaSummary, SourceQuota } from '@/components/settings/models/types';

import { frames, type FrameSpec } from '../../frame';
import { AccountRefresh, ResetCards, type ResetCardRow, type ResetCardsPhase } from '../../kit/quota-reset-cards';
import { DESIGN_NOW } from '../../shell/clock';
import { at, baseApi, chatgptPro, claudeMax, quotaSummary, settingsPaneClass } from './fixtures';

const nov28 = { zh: '11月28日', en: 'Nov 28' };
const dec12 = { zh: '12月12日', en: 'Dec 12' };

const full: ResetCardRow = { kind: 'all', count: 1, expires: dec12 };
const fable: ResetCardRow = { kind: 'fable', count: 3, expires: nov28 };
const chatgpt: ResetCardRow = { kind: 'account', count: 2, expires: nov28 };

type Held = { claude?: ResetCardRow[]; chatgpt?: ResetCardRow[]; state?: ResetCardsPhase; refreshing?: boolean };

// Claude Max has used up its 5-hour limit: the moment a reset card is for. After a use the same
// card shows the reading the Model Hub took by itself.
const summary = ({ reset = false } = {}): QuotaSummary => {
  const base = quotaSummary();
  return {
    ...base,
    sources: base.sources.map((source): SourceQuota => source.source_id !== claudeMax.id ? source : {
      ...source,
      fetched_at: at(reset ? 0 : -2),
      windows: source.windows.map((window) => window.id === 'five_hour'
        ? { ...window, used_pct: reset ? 0 : 100, resets_at: at(reset ? 300 : 130) }
        // Fable's own weekly limit, the one a Fable card gives back.
        : window.id === 'seven_day_opus'
          ? { ...window, id: 'seven_day_overage_included', kind: 'model_weekly' as const, label: 'Fable', scope_model: 'Fable', used_pct: reset ? 0 : 88 }
          : window.id === 'seven_day' && reset ? { ...window, used_pct: 22 } : window),
    }),
  };
};

const quota = ({ claude = [], chatgpt: gpt = [], state, refreshing }: Held, reset = false) => {
  const read = readyRegion(summary({ reset }));
  return () => (
    <div className={settingsPaneClass}>
      <QuotaTab
        quota={read}
        now={DESIGN_NOW}
        onRefresh={() => {}}
        onRetry={() => {}}
        onRequestReauth={() => {}}
        accountAction={(source) => <AccountRefresh refreshing={refreshing && source.source_id === claudeMax.id} />}
        accountSection={(source) => (source.source_id === claudeMax.id
          ? <ResetCards cards={claude} state={state} />
          : source.source_id === chatgptPro.id ? <ResetCards cards={gpt} /> : null)}
      />
    </div>
  );
};

const desktop = (name: string, title: string, held: Held, reset = false): FrameSpec => ({
  name, title: `订阅额度 · 重置卡 · ${title}`, width: 1280, height: 1080, status: 'proposed', fixtures: { api: baseApi }, render: quota(held, reset),
});
const phone = (name: string, title: string, held: Held, reset = false): FrameSpec => ({
  name: `${name}-phone`, title: `订阅额度 · 重置卡 · ${title} · 手机`, width: 390, height: 2000, status: 'proposed', fixtures: { api: baseApi }, render: quota(held, reset),
});

const both = [full, fable];
const afterUse = [full, { ...fable, count: 2 }];

const designFrames = frames([
  desktop('none', '没有重置卡', {}),
  desktop('refreshing', '刷新这个账号', { claude: both, chatgpt: [chatgpt], refreshing: true }),
  desktop('one-kind', '一种', { claude: [fable], chatgpt: [chatgpt] }),
  desktop('both-kinds', '两种', { claude: both, chatgpt: [chatgpt] }),
  desktop('confirm', '使用前', { claude: both, state: { phase: 'confirm', kind: 'fable' } }),
  desktop('using', '使用中', { claude: both, state: { phase: 'using', kind: 'fable' } }),
  desktop('used', '已使用', { claude: afterUse, state: { phase: 'used', kind: 'fable' } }, true),
  desktop('failed', '没有用掉', { claude: both, state: { phase: 'failed', kind: 'fable', reason: 'cooldown' } }),
  desktop('unconfirmed', '结果未确认', { claude: both, state: { phase: 'unconfirmed', kind: 'fable' } }),
  desktop('not-usable', '现在不能用', { claude: [full, { ...fable, blocked: 'needs_limit' }], chatgpt: [{ ...chatgpt, blocked: { cooldown: { zh: '2 小时 10 分钟', en: '2 h 10 min' } } }] }, true),
  phone('both-kinds', '两种', { claude: both, chatgpt: [chatgpt] }),
  phone('confirm', '使用前', { claude: both, state: { phase: 'confirm', kind: 'fable' } }),
  phone('used', '已使用', { claude: afterUse, state: { phase: 'used', kind: 'fable' } }, true),
  phone('unconfirmed', '结果未确认', { claude: both, state: { phase: 'unconfirmed', kind: 'fable' } }),
]);

export default designFrames;
