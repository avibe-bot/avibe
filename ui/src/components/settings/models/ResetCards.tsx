// The 重置卡 block inside an account's quota card (ui/design: models/quota-reset-cards/*).
//
// A view: what the server read (`reset_cards`, its `lock` included) and the page's
// own step for this account (`phase`) decide everything drawn here. One row per kind
// held, with its count, the vendor's own statement of what it does (or nothing),
// and when the first one expires. Using one is the user's own press, said in one
// line before it happens. The server holds one use per account at a time, so its
// `lock` disables every 「使用」 of the account on every open page, and a use whose
// answer never arrived keeps them disabled until the account is read again.
import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Check, LoaderCircle, Ticket } from 'lucide-react';

import { Button } from '@/components/ui/button';
import type { ResetCard, ResetCardKind, ResetCardRefusal, ResetCards } from './types';

/** The page's own step for one account's cards. The server's `lock` is read beside it. */
export type ResetCardPhase =
  /** 「使用」 was pressed: one line says what happens, then 确认使用 or 取消. Nothing is sent yet. */
  | { phase: 'confirm'; kind: ResetCardKind; stated: boolean }
  | { phase: 'using'; kind: ResetCardKind; stated: boolean }
  /** The limits and the count on screen are the reading the server returned with this answer. */
  | { phase: 'used'; kind: ResetCardKind; stated: boolean }
  /** The server says no card was spent. `held`: it refused before asking the vendor (the account was busy, or holds no usable card of this kind). */
  | { phase: 'not_used'; kind: ResetCardKind; reason: ResetCardRefusal | 'held' | null }
  /** No answer came back, so the card may be spent; the account stays held until it is read again. */
  | { phase: 'unconfirmed'; kind: ResetCardKind };

export type ResetCardIntent =
  | { type: 'ask'; kind: ResetCardKind; stated: boolean }
  | { type: 'cancel' }
  | { type: 'confirm'; kind: ResetCardKind };

/** The kinds whose vendor states what they do; ChatGPT's `account` states nothing. */
type StatedKind = Exclude<ResetCardKind, 'account'>;
const NOTE = 'text-[length:var(--model-hub-usage-label-size)]';

type Line = { tone: 'note' | 'done' | 'warn'; text: string } | { tone: 'busy'; text: string };

export const ResetCardsBlock: React.FC<{
  cards?: ResetCards;
  phase?: ResetCardPhase | null;
  now: number;
  /** The quota tab's duration wording (「2 小时 10 分」). */
  duration: (ms: number) => string;
  onIntent?: (intent: ResetCardIntent) => void;
}> = ({ cards, phase = null, now, duration, onIntent }) => {
  const { t, i18n } = useTranslation();
  const rows = cards?.cards ?? [];
  const lock = cards?.lock ?? null;
  const noteId = React.useId();
  const focusNext = React.useRef<string | null>(null);
  const block = React.useRef<HTMLElement>(null);
  React.useLayoutEffect(() => {
    const target = focusNext.current;
    focusNext.current = null;
    if (target) block.current?.querySelector<HTMLElement>(`[data-reset-focus="${target}"]`)?.focus();
  });

  const name = (kind: ResetCardKind) => t(`settings.models.quota.resetCards.kind.${kind}`) as string;
  /** The vendor's own statement applies to this kind, or nothing is said. */
  const stated = (kind: ResetCardKind, isStated: boolean): kind is StatedKind => isStated && kind !== 'account';
  const day = (iso: string) => {
    const at = Date.parse(iso);
    if (Number.isNaN(at)) return null;
    const sameYear = new Date(at).getFullYear() === new Date(now).getFullYear();
    return new Intl.DateTimeFormat(i18n.language, {
      month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }),
    }).format(at);
  };
  const blockedText = (card: ResetCard) => {
    if (card.usable) return null;
    if (card.blocked === 'needs_limit') return t('settings.models.quota.resetCards.blocked.needs_limit') as string;
    if (card.blocked === 'cooldown') {
      const until = card.blocked_until !== null ? Date.parse(card.blocked_until) - now : NaN;
      return until > 0
        ? t('settings.models.quota.resetCards.blocked.cooldown', { duration: duration(until) }) as string
        : t('settings.models.quota.resetCards.blocked.cooldownUnknown') as string;
    }
    return t('settings.models.quota.resetCards.blocked.unavailable') as string;
  };

  // What is in hand for the account: the server's lock first, then this page's step.
  const usingKind = lock?.reason === 'in_flight' ? lock.kind : phase?.phase === 'using' ? phase.kind : null;
  const unconfirmedKind = lock?.reason === 'unconfirmed' ? lock.kind : phase?.phase === 'unconfirmed' ? phase.kind : null;
  const held = lock !== null || phase?.phase === 'using' || phase?.phase === 'unconfirmed';
  const asking = phase?.phase === 'confirm' && !held ? phase : null;

  const line = (kind: ResetCardKind): Line | null => {
    if (unconfirmedKind === kind) return { tone: 'warn', text: t('settings.models.quota.resetCards.unconfirmed') as string };
    if (usingKind !== null || unconfirmedKind !== null) return null;
    if (asking?.kind === kind) return { tone: 'note', text: t('settings.models.quota.resetCards.confirmNote') as string };
    if (phase?.kind !== kind) return null;
    if (phase.phase === 'used') {
      return {
        tone: 'done',
        text: stated(kind, phase.stated)
          ? t('settings.models.quota.resetCards.usedDone', { done: t(`settings.models.quota.resetCards.done.${kind}`) }) as string
          : t('settings.models.quota.resetCards.used') as string,
      };
    }
    if (phase.phase === 'not_used') {
      return { tone: 'warn', text: t(`settings.models.quota.resetCards.notUsed.${phase.reason ?? 'other'}`) as string };
    }
    return null;
  };

  // A message about a kind no longer listed (its last card was used) is said under the list.
  const listed = new Set(rows.map((card) => card.kind));
  const aboutKind = unconfirmedKind ?? usingKind ?? (phase && phase.phase !== 'confirm' ? phase.kind : null);
  const orphan: Line | null = aboutKind === null || listed.has(aboutKind)
    ? null
    : usingKind === aboutKind && unconfirmedKind === null
      ? { tone: 'busy', text: t('settings.models.quota.resetCards.using') as string }
      : line(aboutKind);

  if (rows.length === 0 && orphan === null) return null;

  const intent = (next: ResetCardIntent, focus: string) => {
    focusNext.current = focus;
    onIntent?.(next);
  };

  return (
    <section ref={block} className="mt-3.5 rounded-lg border border-border bg-surface" aria-label={t('settings.models.quota.resetCards.title') as string} data-reset-cards>
      <h3 className={`flex items-center gap-1.5 border-b border-border px-3 py-2 font-medium text-muted ${NOTE}`}>
        <Ticket className="size-3.5" aria-hidden />
        {t('settings.models.quota.resetCards.title')}
      </h3>
      {rows.length > 0 && (
        <ul className="divide-y divide-border">
          {rows.map((card) => {
            const kindName = name(card.kind);
            const blocked = blockedText(card);
            const message = line(card.kind);
            const confirming = asking?.kind === card.kind;
            const expires = card.expires_at !== null ? day(card.expires_at) : null;
            return (
              <li key={card.kind} className="px-3 py-2.5" data-reset-card={card.kind}>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                  <div className="min-w-0 flex-1 basis-56">
                    <div className="flex items-baseline gap-1.5 text-xs font-medium text-foreground">
                      <span className="truncate">{kindName}</span>
                      <span className="shrink-0 tabular-nums text-muted">{t('settings.models.quota.resetCards.count', { number: card.count })}</span>
                    </div>
                    {stated(card.kind, card.stated) && (
                      <p className={`mt-0.5 text-muted ${NOTE}`}>{t(`settings.models.quota.resetCards.effect.${card.kind}`)}</p>
                    )}
                    {/* Its own line, so the date never breaks inside itself at any width. */}
                    {expires && (
                      <p className={`mt-0.5 whitespace-nowrap text-muted ${NOTE}`} data-reset-card-expiry>
                        {t('settings.models.quota.resetCards.expires', { date: expires })}
                      </p>
                    )}
                  </div>
                  {confirming ? (
                    <div className="flex shrink-0 items-center gap-2">
                      <Button size="compact" aria-describedby={noteId} onClick={() => intent({ type: 'confirm', kind: card.kind }, '')}>
                        {t('settings.models.quota.resetCards.confirm')}
                      </Button>
                      <Button size="compact" variant="ghost" aria-describedby={noteId} data-reset-focus={`cancel:${card.kind}`} onClick={() => intent({ type: 'cancel' }, `use:${card.kind}`)}>
                        {t('settings.models.quota.resetCards.cancel')}
                      </Button>
                    </div>
                  ) : usingKind === card.kind && unconfirmedKind === null ? (
                    <Button size="compact" variant="outline" className="shrink-0" disabled>
                      <LoaderCircle className="animate-spin" aria-hidden />
                      {t('settings.models.quota.resetCards.using')}
                    </Button>
                  ) : blocked ? (
                    <span className={`shrink-0 text-muted ${NOTE}`} data-reset-card-blocked={card.blocked ?? 'unavailable'}>{blocked}</span>
                  ) : (
                    <Button
                      size="compact"
                      variant="outline"
                      className="shrink-0"
                      data-reset-focus={`use:${card.kind}`}
                      aria-label={t('settings.models.quota.resetCards.useNamed', { name: kindName }) as string}
                      disabled={held || (asking !== null && !confirming)}
                      onClick={() => intent({ type: 'ask', kind: card.kind, stated: card.stated }, `cancel:${card.kind}`)}
                    >
                      {t('settings.models.quota.resetCards.use')}
                    </Button>
                  )}
                </div>
                {message && <Message line={message} id={confirming ? noteId : undefined} />}
              </li>
            );
          })}
        </ul>
      )}
      {orphan && <div className="px-3 pb-2.5 pt-0.5"><Message line={orphan} /></div>}
    </section>
  );
};

const Message: React.FC<{ line: Line; id?: string }> = ({ line, id }) => {
  if (line.tone === 'note') return <p id={id} className={`mt-2 text-foreground ${NOTE}`} role="status">{line.text}</p>;
  if (line.tone === 'busy') {
    return (
      <p className={`mt-2 flex items-center gap-1.5 text-muted ${NOTE}`} role="status">
        <LoaderCircle className="size-3.5 shrink-0 animate-spin" aria-hidden />{line.text}
      </p>
    );
  }
  if (line.tone === 'done') {
    return (
      <p className={`mt-2 flex items-center gap-1.5 model-hub-ink-mint ${NOTE}`} role="status">
        <Check className="size-3.5 shrink-0" aria-hidden />{line.text}
      </p>
    );
  }
  return (
    <p className={`mt-2 flex items-start gap-1.5 text-destructive-ink ${NOTE}`} role="alert">
      <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden />{line.text}
    </p>
  );
};
