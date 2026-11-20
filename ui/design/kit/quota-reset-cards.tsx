// Proposal: reset cards inside an account's quota card (订阅额度), and a refresh on that card.
//
// What a vendor reports per card decides everything drawn here: its kind, how many are held, when
// the first one expires, and whether one can be used now. Nothing is drawn for an account that
// holds none. Using a card is the user's own press, said in one line before it happens, and the
// account's limits and cards are read again by themselves afterwards.
//
// The line under a card's name is the vendor's own statement of what the card does, never ours:
// Anthropic's help page words for its kinds (support.claude.com "What is a limit reset?"), checked
// against the limits the card's own entry lists. A card whose vendor states nothing (ChatGPT's)
// shows its name, count and expiry only. After a use or a refusal the count shown is the one read
// again from the vendor; "not used" is said only when that count confirms it.
import { AlertTriangle, Check, LoaderCircle, RefreshCw, Ticket } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

import { useDraft } from './draft';

/** What a card resets, as the vendor states it. `all`, `fable`, `session`, `weekly` are Anthropic's; `account` is ChatGPT's one kind. */
export type ResetCardKind = 'all' | 'fable' | 'session' | 'weekly' | 'account';

export interface ResetCardRow {
  kind: ResetCardKind;
  count: number;
  /** The first of these to expire, as drawn (「11月28日」 / "Nov 28"); absent when the vendor names none. */
  expires?: { zh: string; en: string };
  /** Why none of this kind can be used now, when the vendor says so. */
  blocked?: 'needs_limit' | { cooldown: { zh: string; en: string } };
}

export type ResetCardsPhase =
  | { phase: 'idle' }
  /** The user pressed 使用: one line says what happens, then 确认使用 or 取消. */
  | { phase: 'confirm'; kind: ResetCardKind }
  | { phase: 'using'; kind: ResetCardKind }
  /** Used: said in the vendor's words for the kind. The limits and the count are already the fresh reading. */
  | { phase: 'used'; kind: ResetCardKind }
  /** Not used: the vendor refused, and the count read again afterwards (the one this row shows) confirms no card was spent. */
  | { phase: 'failed'; kind: ResetCardKind; reason: 'not_limited' | 'cooldown' | 'refused' }
  /** The vendor's answer never arrived, or the count could not be read after a refusal: the card may or may not be spent, so no card of this account can be used until it is read again. */
  | { phase: 'unconfirmed'; kind: ResetCardKind };

const useCopy = () => {
  const d = useDraft();
  const name = (kind: ResetCardKind) => ({
    all: d('全部额度重置卡', 'Full reset'),
    fable: d('Fable 重置卡', 'Fable reset'),
    session: d('5 小时重置卡', '5-hour reset'),
    weekly: d('每周重置卡', 'Weekly reset'),
    account: d('重置卡', 'Limit reset'),
  })[kind];
  const effect = (kind: ResetCardKind) => ({
    all: d('5 小时额度和每周额度回满', 'The 5-hour and weekly limits go back to full'),
    fable: d('退回本周用在 Fable 上的额度，其他模型用掉的不退；5 小时额度回满', 'Gives back the weekly usage spent on Fable, not on other models; the 5-hour limit is fully refilled'),
    session: d('5 小时额度回满', 'The 5-hour limit goes back to full'),
    weekly: d('每周额度回满', 'The weekly limit goes back to full'),
    account: null,
  })[kind];
  const done = (kind: ResetCardKind) => ({
    all: d('5 小时额度和每周额度已回满', 'The 5-hour and weekly limits are back to full'),
    fable: d('本周用在 Fable 上的额度已退回，5 小时额度已回满', 'The weekly usage spent on Fable is given back; the 5-hour limit is refilled'),
    session: d('5 小时额度已回满', 'The 5-hour limit is back to full'),
    weekly: d('每周额度已回满', 'The weekly limit is back to full'),
    account: null,
  })[kind];
  return { d, name, effect, done };
};

/** The control beside an account's status: reads this account again at the vendor. */
export function AccountRefresh({ refreshing = false }: { refreshing?: boolean }) {
  const d = useDraft();
  return (
    <Button variant="ghost" size="icon" className="size-7 shrink-0 text-muted" disabled={refreshing} aria-label={d('刷新这个账号', 'Refresh this account')}>
      <RefreshCw className={cn('size-3.5', refreshing && 'animate-spin')} aria-hidden />
    </Button>
  );
}

export function ResetCards({ cards, state = { phase: 'idle' } }: { cards: ResetCardRow[]; state?: ResetCardsPhase }) {
  const { d, name, effect, done } = useCopy();
  if (cards.length === 0) return null;
  const busy = state.phase === 'using';
  const acting = state.phase === 'idle' ? null : state.kind;
  return (
    <section className="mt-3.5 rounded-lg border border-border bg-surface" aria-label={d('重置卡', 'Limit resets')}>
      <h3 className="flex items-center gap-1.5 border-b border-border px-3 py-2 text-[length:var(--model-hub-usage-label-size)] font-medium text-muted">
        <Ticket className="size-3.5" aria-hidden />
        {d('重置卡', 'Limit resets')}
      </h3>
      <ul className="divide-y divide-border">
        {cards.map((card) => {
          const mine = acting === card.kind;
          const blocked = card.blocked === 'needs_limit'
            ? d('到限额后才能用', 'Usable once a limit is reached')
            : card.blocked
              ? d(`${card.blocked.cooldown.zh}后可用`, `Usable in ${card.blocked.cooldown.en}`)
              : null;
          return (
            <li key={card.kind} className="px-3 py-2.5">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                <div className="min-w-0 flex-1 basis-56">
                  <div className="flex items-baseline gap-1.5 text-xs font-medium text-foreground">
                    <span className="truncate">{name(card.kind)}</span>
                    <span className="shrink-0 tabular-nums text-muted">×{card.count}</span>
                  </div>
                  {(effect(card.kind) || card.expires) && (
                    <p className="mt-0.5 text-[length:var(--model-hub-usage-label-size)] text-muted">
                      {[effect(card.kind), card.expires && d(`最早 ${card.expires.zh} 到期`, `First expires ${card.expires.en}`)].filter(Boolean).join(' · ')}
                    </p>
                  )}
                </div>
                {mine && state.phase === 'confirm' ? (
                  <div className="flex shrink-0 items-center gap-2">
                    <Button size="compact">{d('确认使用', 'Use it')}</Button>
                    <Button size="compact" variant="ghost">{d('取消', 'Cancel')}</Button>
                  </div>
                ) : mine && state.phase === 'using' ? (
                  <Button size="compact" variant="outline" className="shrink-0" disabled>
                    <LoaderCircle className="size-3.5 animate-spin" aria-hidden />
                    {d('使用中…', 'Using…')}
                  </Button>
                ) : blocked ? (
                  <span className="shrink-0 text-[length:var(--model-hub-usage-label-size)] text-muted">{blocked}</span>
                ) : (
                  <Button size="compact" variant="outline" className="shrink-0" disabled={busy || state.phase === 'unconfirmed' || (acting !== null && !mine && state.phase === 'confirm')}>
                    {d('使用', 'Use')}
                  </Button>
                )}
              </div>
              {mine && state.phase === 'confirm' && (
                <p className="mt-2 text-[length:var(--model-hub-usage-label-size)] text-foreground" role="status">
                  {d('用掉 1 张，不能撤销。', 'Spends 1. It can’t be undone.')}
                </p>
              )}
              {mine && state.phase === 'used' && (
                <p className="mt-2 flex items-center gap-1.5 text-[length:var(--model-hub-usage-label-size)] text-mint-ink" role="status">
                  <Check className="size-3.5 shrink-0" aria-hidden />
                  {done(card.kind) ? d(`用了 1 张。${done(card.kind)}。`, `1 used. ${done(card.kind)}.`) : d('用了 1 张。', '1 used.')}
                </p>
              )}
              {mine && state.phase === 'failed' && (
                <p className="mt-2 flex items-start gap-1.5 text-[length:var(--model-hub-usage-label-size)] text-destructive-ink" role="alert">
                  <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden />
                  {state.reason === 'not_limited'
                    ? d('没有用掉：这个账号现在没到限额。', 'Not used: this account isn’t at a limit now.')
                    : state.reason === 'cooldown'
                      ? d('没有用掉：刚用过一张，要等一会儿才能再用。', 'Not used: one was used recently, and the next has to wait.')
                      : d('没有用掉：供应商拒绝了这次请求。', 'Not used: the provider refused the request.')}
                </p>
              )}
              {mine && state.phase === 'unconfirmed' && (
                <p className="mt-2 flex items-start gap-1.5 text-[length:var(--model-hub-usage-label-size)] text-destructive-ink" role="alert">
                  <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden />
                  {d('没收到供应商的结果，这张卡可能已经用掉。先别再用，过一会儿刷新这个账号看张数。', 'The provider’s answer didn’t arrive, so this card may be spent. Don’t use another yet; refresh this account in a moment and check the count.')}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
