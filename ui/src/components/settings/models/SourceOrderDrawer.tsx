// 来源顺序 · <Agent> — the per-Agent source order editor (design.pen 「产品改造
// V6 02」 desktop-custom, 「V6 03」 desktop-follow, 「V6 M02」 mobile).
//
// V6's central move: an order is a property of an AGENT, not of the source
// inventory. Each backend holds an ordered SUBSET of the eligible sources, so
// this drawer owns three groups — 启用 (the ordered subset, drag to reorder),
// 未启用 (eligible but left out), 不适用 (ineligible here, with the server's
// reason) — and the policy state machine behind them:
//
//   follow  跟随推荐   server-computed order; a newly added source joins it
//   custom  自定义     user-owned subset; new sources do NOT join it
//
// Any manual edit under `follow` forks to `custom` implicitly (that is what the
// user just did), and 恢复推荐顺序 goes back. Every edit persists immediately
// instead of batching behind 完成, unlike the sibling menu drawers: ● 当前 and
// 暂不可用 are server-derived, so a held draft would either show a stale head or
// force re-deriving §4.3's resolution in the UI to keep those pills honest.
// 完成 just closes.
//
// Reachable in Hub mode only (AC-7): a Direct backend has no Hub order, no chain
// and no probe, so the affordance is withdrawn rather than shown empty.
import * as React from 'react';
import type { TFunction } from 'i18next';
import { Reorder, useDragControls } from 'framer-motion';
import {
  CheckCircle2,
  ChevronRight,
  CirclePlus,
  GripVertical,
  List,
  Loader2,
  TriangleAlert,
  WandSparkles,
  X,
  Zap,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { useIsMobile } from '@/lib/useIsMobile';
import { useToast } from '@/context/ToastContext';
import { initialSeedState, savedSourcesKey, seedStep } from './asyncLifetime';
import { CurrentChip, StateChip } from './chips';
import { eligibilityOf } from './eligibility';
import { DRY_RUN_ENABLED } from './featureFlags';
import { cooldownEtaMinutes } from './format';
import { MenuDrawer } from './menus/MenuDrawer';
import { apiFailure, modelsApi } from './modelsApi';
import { dryRunOutcome, dryRunPlan, dryRunRowView, probeArrival, type DryRunOutcome } from './repair';
import { movedOrder, sameIds } from './reorder';
import { serverText } from './serverCopy';
import { orderSufficiency } from './sufficiency';
import { isUnhealthy, needsAttention } from './supply';
import { ACCENT_ICON, ACCENT_TILE, backendVisual, sourceVisual } from './vendorMeta';
import type { AgentBackend, AgentSourcesPut, AgentSupply, Source, SourcePolicy } from './types';

// ── Row parts ───────────────────────────────────────────────────────────
//
// The three row shapes share one geometry so their tiles and names line up down
// the list: the leading column is a 16–18px glyph (grip / ⊕ / nothing) and the
// gaps do the rest, which reproduces the frames' 12→38→68 (phone) and
// 14→42→76→122 (desktop) columns exactly.
const ROW = 'flex items-center gap-2.5 rounded-xl border px-3 py-2.5 sm:gap-3 sm:px-3.5 sm:py-3';
const NUMBER =
  'grid size-5 shrink-0 place-items-center rounded-md border border-border bg-foreground/[0.03] text-[10.5px] font-bold text-muted sm:size-[22px] sm:rounded-[7px] sm:text-[11px]';

const SourceTile: React.FC<{ source: Source }> = ({ source }) => {
  const { Icon, accent } = sourceVisual(source);
  return (
    <span
      className={cn(
        'flex size-[30px] shrink-0 items-center justify-center rounded-[9px] sm:size-[34px] sm:rounded-[10px]',
        ACCENT_TILE[accent],
      )}
    >
      <Icon className={cn('size-3.5 sm:size-4', ACCENT_ICON[accent])} />
    </span>
  );
};

/**
 * Name over sub-line. Gold on the same predicate as the source list's sub-line
 * (`needsAttention`): one source cannot mean two different things on two surfaces,
 * and here the 暂不可用 pill already carries "not serving right now", so gold is
 * reserved for "and it needs you".
 */
const Identity: React.FC<{ name: string; subline: string; amber?: boolean }> = ({ name, subline, amber }) => (
  <span className="flex min-w-0 flex-1 flex-col gap-0.5 sm:gap-[3px]">
    <span className="truncate text-[13px] font-semibold text-foreground sm:text-[13.5px]">{name}</span>
    {subline && (
      <span className={cn('truncate text-[10px] sm:text-[11px]', amber ? 'text-gold' : 'text-muted')}>{subline}</span>
    )}
  </span>
);

const GroupHeader: React.FC<{ label: string; first?: boolean; children?: React.ReactNode }> = ({
  label,
  first,
  children,
}) => (
  <div className={cn('flex items-center justify-between gap-3 px-1 sm:pt-1', first ? 'pt-0.5 sm:pt-0' : 'pt-2')}>
    <span className="text-[10px] font-bold tracking-[1px] text-muted">{label}</span>
    {children}
  </div>
);

// ── Drawer ──────────────────────────────────────────────────────────────

export const SourceOrderDrawer: React.FC<{
  open: boolean;
  agent: AgentSupply;
  /** Every Agent's supply — read only to name the client a wrong-client
   *  subscription DOES serve (see `sanctionedBackend`). */
  agents: AgentSupply[];
  sources: Source[];
  onClose: () => void;
  /** Re-read sources + agents: an order change moves ● 当前 on the page too. */
  onSaved: () => void;
  /** Desktop footer 模型菜单与映射 — hand off to this backend's menu drawer.
   *  Omitted while the menus are flagged off. */
  onOpenMenu?: () => void;
}> = ({ open, agent, agents, sources, onClose, onSaved, onOpenMenu }) => {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { Icon, accent } = backendVisual(agent.backend);
  // The frames shorten three strings for the 390px sheet — the subtitle, the
  // 未启用 mapping note and the 跟随推荐中 badge — and at full length the first two
  // wrap to three lines and truncate mid-word respectively, so the shortening is
  // load-bearing rather than cosmetic. A JS branch, against `useIsMobile`'s own
  // advice to prefer responsive classes, because two of the three are composed
  // strings: the subtitle is a `string` prop of the shared shell, and the note is
  // one ` · `-joined segment of a sub-line. Rendering both variants would mean
  // duplicating the join, so the whole file takes its short copy from here.
  const mobile = useIsMobile();

  const [policy, setPolicy] = React.useState<SourcePolicy>(agent.sources?.policy ?? 'follow');
  const [order, setOrder] = React.useState<string[]>(agent.sources?.order ?? []);
  const [saving, setSaving] = React.useState(false);
  /** Last state the server confirmed — the revert target and the no-op test.
   *  `order` alone can serve neither: a drag has already moved it by the time
   *  the commit fires. */
  const saved = React.useRef<{ policy: SourcePolicy; order: string[] }>({ policy, order });

  // Seed from the saved order the server holds. `seedStep` owns *when*, because
  // when is the part that has to survive a save landing after the drawer that
  // issued it was closed and reopened — see asyncLifetime.ts.
  const seed = React.useRef(initialSeedState);
  const authoritative = savedSourcesKey(agent);
  React.useEffect(() => {
    if (!open) return;
    const step = seedStep(seed.current, authoritative);
    seed.current = step.state;
    if (!step.reseed) return;
    const next = { policy: agent.sources?.policy ?? 'follow', order: agent.sources?.order ?? [] };
    saved.current = next;
    setPolicy(next.policy);
    setOrder(next.order);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, authoritative]);

  const backendLabel = (backend: string) =>
    t(`settings.models.backends.${backend}`, { defaultValue: backend }) as string;
  const backendName = backendLabel(agent.backend);
  const byId = React.useMemo(() => new Map(sources.map((s) => [s.id, s])), [sources]);
  const builtins = React.useMemo(() => new Set(agent.builtin_models ?? []), [agent.builtin_models]);

  // Everything below is driven by `enabledSources` / `enabledIds` rather than by
  // `order` directly: an id in the order with no matching source (a row deleted
  // between the two reads) simply drops out, and the next commit persists the
  // pruned list — the same self-heal the server's own sync performs.
  const enabledSources = order.map((id) => byId.get(id)).filter((s): s is Source => s !== undefined);
  const enabledIds = enabledSources.map((s) => s.id);
  // Asked about the order the user is CURRENTLY editing, not about the saved one —
  // the warning is about what pressing 保存 would leave this backend with.
  const orderVerdict = orderSufficiency(order, sources);
  const rest = sources.filter((s) => !order.includes(s.id));
  const disabledSources = rest.filter((s) => eligibilityOf(agent, s.id).eligible);
  const ineligible = rest
    .map((source) => ({ source, ...eligibilityOf(agent, source.id) }))
    .filter((e) => !e.eligible);

  /**
   * Sub-line vocabulary (design.pen V6 02/03) — always at most two segments:
   *
   *   启用    `me@gmail.com · 供 Claude 全系`, `key …9c1 · 47 分后重试`
   *   未启用  `供 GLM 全系 · 经模型菜单改写后可供 Claude 使用`
   *   不适用  the server's eligibility reason, alone
   *
   * The 供 segment names a model FAMILY, which is a vendor label, so a source with
   * no family (a relay / custom endpoint) omits it rather than inventing one. The
   * mapping note is read off the payload — none of this backend's own built-in ids
   * is supplied here, so the source is only reachable through a 模型菜单 rewrite —
   * never from a UI-side guess about which vendor belongs to which backend, which
   * is the mirror this lane deletes.
   */
  const vendorLabel = (source: Source) =>
    t(`settings.models.addKey.vendors.${source.vendor}`, { defaultValue: source.vendor }) as string;
  const supplies = (source: Source) => {
    const family = t(`settings.models.order.family.${source.vendor}`, { defaultValue: '' }) as string;
    return family ? (t('settings.models.order.supplies', { family }) as string) : '';
  };
  const identity = (source: Source) => source.account_label ?? source.masked_credential ?? '';
  const join = (parts: (string | false)[]) => parts.filter(Boolean).join(' · ');

  const enabledSubline = (source: Source): string =>
    join([
      identity(source),
      // A cooling source spends its second segment on the ETA instead of the
      // family — the chip says 暂不可用, this says until when.
      source.state.status === 'cooldown'
        ? (t('settings.models.source.retryIn', { minutes: cooldownEtaMinutes(source.state.retry_at) }) as string)
        : isUnhealthy(source.state)
          ? ''
          : supplies(source),
    ]);

  const disabledSubline = (source: Source): string => {
    // Only a fixed-menu backend rewrites ids through mappings; the open-menu
    // backend picks a supplied id directly, so the note would be false there.
    const viaMapping =
      agent.menu_kind === 'fixed' && builtins.size > 0 && !source.models.some((m) => builtins.has(m.id));
    return viaMapping
      ? join([
          supplies(source),
          t(mobile ? 'settings.models.order.viaMappingShort' : 'settings.models.order.viaMapping', {
            backend: backendName,
          }) as string,
        ])
      : join([identity(source), supplies(source)]);
  };

  /**
   * The one OTHER backend this source may serve, or null when that isn't a single
   * definite answer. Read off the other Agents' server-published eligibility —
   * the vendor→sanctioned-client map is exactly the mirror this lane deleted, so
   * when the payload can't name the client the copy doesn't either.
   */
  const sanctionedBackend = (sourceId: string): string | null => {
    const owners = agents.filter((a) => a.backend !== agent.backend && eligibilityOf(a, sourceId).eligible);
    return owners.length === 1 ? backendLabel(owners[0].backend) : null;
  };

  const ineligibleSubline = (source: Source, reasonKey: string | null): string => {
    if (!reasonKey) {
      // A payload with no reason still grays the row out — better than offering an
      // 启用 the PUT would reject with `invalid_source_order`.
      return t('settings.models.order.ineligibleUnknown', { backend: backendName }) as string;
    }
    const vendor = vendorLabel(source);
    if (reasonKey === 'models.eligibility.subscription_wrong_client') {
      const client = sanctionedBackend(source.id);
      return client
        ? (t(reasonKey, { vendor, backend: client }) as string)
        : (t('settings.models.order.ineligibleClientUnknown', { vendor }) as string);
    }
    // The server's own i18n key (contract v3's closed vocabulary), rendered as-is.
    return t(reasonKey, { vendor }) as string;
  };

  /**
   * Persist one edit, showing it immediately and rolling back if the server
   * refuses. The echoed AgentSupply always wins over the optimistic guess: under
   * `follow` the server owns the order outright, so 恢复推荐顺序 learns the
   * recommended order from the response and nowhere else.
   *
   * Two edits in flight resolve last-write-wins, which is correct here because
   * every PUT carries the whole list rather than a delta.
   *
   * Runs to completion even if the drawer closes mid-flight, and deliberately so.
   * Both of the things this does on the way out belong to components that stay
   * mounted — `onSaved()` re-reads the page whose ● 当前 the write just moved, and
   * the failure toast lives at the app root — so a mounted-ref guard here would
   * not be protecting anything, it would be dropping the only report that a
   * rejected reorder ever gets. (The setState calls need no guard either: since
   * React 18 they are a no-op on an unmounted component, not a warning.)
   */
  const persist = async (body: AgentSourcesPut, next: { policy: SourcePolicy; order: string[] }) => {
    const previous = saved.current;
    setPolicy(next.policy);
    setOrder(next.order);
    setSaving(true);
    try {
      // No `contract_version` in the body — the route rejects unknown keys.
      const echoed = await modelsApi.putAgentSources(agent.backend, body);
      const adopted = {
        policy: echoed.sources?.policy ?? next.policy,
        order: echoed.sources?.order ?? next.order,
      };
      saved.current = adopted;
      setPolicy(adopted.policy);
      setOrder(adopted.order);
      onSaved();
    } catch {
      saved.current = previous;
      setPolicy(previous.policy);
      setOrder([...previous.order]);
      showToast(t('settings.models.toast.reorderFailed') as string, 'error');
    } finally {
      setSaving(false);
    }
  };

  // Any manual edit is a fork to `custom`: the user just said which sources, in
  // which order — that is the definition of a user-owned subset.
  //
  // Which makes the no-op test policy-INDEPENDENT, because `follow` is the policy
  // that has something to lose. Both edit paths can arrive here having changed
  // nothing: `movedOrder` returns the list untouched at either boundary, and a drag
  // can land back where it started. Forking a backend off the server's
  // recommendation because someone pressed ArrowUp on row 1 is a silent, invisible
  // consequence for an input that did nothing.
  const commitOrder = (nextOrder: string[]) => {
    if (sameIds(saved.current.order, nextOrder)) {
      // Still re-seat local state: a drag has already moved `order` past the saved
      // list by the time this fires, and the row it dropped on must snap back.
      setOrder([...saved.current.order]);
      return;
    }
    void persist({ policy: 'custom', order: nextOrder }, { policy: 'custom', order: nextOrder });
  };

  const restoreRecommended = () => void persist({ policy: 'follow' }, { policy: 'follow', order });

  return (
    <MenuDrawer
      open={open}
      onClose={onClose}
      Icon={Icon}
      accent={accent}
      title={t('settings.models.order.title', { backend: backendName }) as string}
      subtitle={
        t(mobile ? 'settings.models.order.subtitleShort' : 'settings.models.order.subtitle', {
          backend: backendName,
        }) as string
      }
      footer={
        <>
          <div className="flex min-w-0 items-center gap-2.5">
            {/* 恢复推荐顺序 lives in the 启用 group header on desktop (V6 02) and
                in the footer on phones (M02), where that header has no room. */}
            {policy === 'custom' && (
              <Button
                variant="secondary"
                className="h-[46px] rounded-xl px-4 text-[13px] font-semibold text-muted sm:hidden"
                onClick={restoreRecommended}
                disabled={saving}
              >
                {t('settings.models.order.restore')}
              </Button>
            )}
            {/* Desktop home for 模型菜单与映射 (V6 02's footer). The phone's home is
                the row at the end of the list — M02 gives this footer to 恢复推荐
                顺序 + 完成, and there is no width where the flow may be missing. */}
            {onOpenMenu && (
              <Button variant="outline" size="sm" className="hidden sm:inline-flex" onClick={onOpenMenu}>
                <List className="size-3.5" />
                {t('settings.models.order.openMenu')}
              </Button>
            )}
          </div>
          <Button
            variant="brand"
            size="sm"
            className="h-[46px] flex-1 rounded-xl text-[13.5px] sm:h-9 sm:flex-none sm:rounded-[10px] sm:px-[18px] sm:text-[12.5px]"
            onClick={onClose}
          >
            {t('settings.models.menus.done')}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-2 sm:gap-2.5">
        <GroupHeader label={t('settings.models.order.groupEnabled', { count: enabledIds.length }) as string} first>
          {policy === 'custom' ? (
            <button
              type="button"
              onClick={restoreRecommended}
              disabled={saving}
              className="hidden shrink-0 text-[12px] font-semibold text-cyan transition-colors hover:text-cyan/80 disabled:opacity-50 sm:inline"
            >
              {t('settings.models.order.restore')}
            </button>
          ) : (
            <Badge variant="info" className="shrink-0 gap-1 py-1 text-[10px] font-semibold sm:gap-1.5 sm:text-[11px]">
              <WandSparkles className="size-2.5 sm:size-3" />
              {/* The phone frame drops the trailing clause for width, not meaning. */}
              {t(mobile ? 'settings.models.order.following' : 'settings.models.order.followingLong')}
            </Badge>
          )}
        </GroupHeader>

        {orderVerdict.kind === 'adopted_none' ? (
          // V6 doesn't draw this state, but emptying the list is a legal thing to
          // ask for (§4.4: an omitted id IS 未启用) — an unexplained empty area
          // would be worse than one line saying what it means.
          <p className="rounded-xl border border-gold/40 bg-gold/[0.08] px-3.5 py-3 text-[12.5px] leading-relaxed text-gold">
            {t('settings.models.order.enabledEmpty', { backend: backendName })}
          </p>
        ) : (
          <Reorder.Group axis="y" values={enabledIds} onReorder={setOrder} className="flex list-none flex-col gap-2 sm:gap-2.5">
            {orderVerdict.kind === 'nothing_runnable' && (
              // A full list is not a working list. Each row already carries its own
              // state chip, but reading five 暂不可用 chips and concluding 「so the
              // next turn fails」 is the user doing the rollup by hand — and the row
              // that says it is the one nobody reads, because the list looks fine.
              <li className="list-none rounded-xl border border-gold/40 bg-gold/[0.08] px-3.5 py-3 text-[12.5px] leading-relaxed text-gold">
                {t('settings.models.order.enabledNoneRunnable', { backend: backendName })}
              </li>
            )}
            {enabledSources.map((source, index) => (
              <EnabledRow
                key={source.id}
                source={source}
                index={index}
                isCurrent={agent.current?.source_id === source.id}
                subline={enabledSubline(source)}
                busy={saving}
                onCommit={() => commitOrder(enabledIds)}
                onMove={(delta) => commitOrder(movedOrder(enabledIds, index, delta))}
                onRemove={() => commitOrder(enabledIds.filter((id) => id !== source.id))}
              />
            ))}
          </Reorder.Group>
        )}

        {disabledSources.length > 0 && (
          <>
            <GroupHeader label={t('settings.models.order.groupDisabled', { count: disabledSources.length }) as string} />
            {disabledSources.map((source) => (
              // The whole row enables the source: on a phone the frame's 44×28
              // button is a mis-tap, and "enable this source" is one intent, so the
              // row is the control and the pill is its label (no nested button).
              <button
                key={source.id}
                type="button"
                aria-label={t('settings.models.order.enableAria', { name: source.display_name }) as string}
                onClick={() => commitOrder([...enabledIds, source.id])}
                disabled={saving}
                className={cn(ROW, 'w-full border-border text-left opacity-85 transition hover:opacity-100 disabled:opacity-50')}
              >
                <CirclePlus className="size-[17px] shrink-0 text-mint sm:size-[18px]" aria-hidden />
                <SourceTile source={source} />
                <Identity name={source.display_name} subline={disabledSubline(source)} />
                <span className="shrink-0 rounded-lg border border-border-strong bg-card px-3 py-1.5 text-[11px] font-semibold text-foreground sm:text-[11.5px]">
                  {t('settings.models.order.enable')}
                </span>
              </button>
            ))}
          </>
        )}

        {ineligible.length > 0 && (
          <>
            <GroupHeader label={t('settings.models.order.groupIneligible', { count: ineligible.length }) as string} />
            {ineligible.map(({ source, reasonKey }) => (
              <div key={source.id} className={cn(ROW, 'border-border opacity-[0.55]')}>
                <SourceTile source={source} />
                <Identity name={source.display_name} subline={ineligibleSubline(source, reasonKey)} />
              </div>
            ))}
          </>
        )}

        {DRY_RUN_ENABLED && (
          <DryRunRow
            agent={agent}
            sources={sources}
            chainKey={`${policy}|${order.join('>')}`}
            saving={saving}
            reread={onSaved}
          />
        )}

        {/* The phone's home for 模型菜单与映射. A frame that doesn't draw a control
            at 390px is saying where to move it, not that a phone user may not
            have it — the two configuration surfaces answer adjacent questions
            (which sources, which models) and this drawer is the only way into
            the second one now that the Agent row's action is 来源顺序. */}
        {onOpenMenu && (
          <Button
            variant="outline"
            className="mt-1 h-[46px] w-full justify-between rounded-xl px-4 text-[13px] font-semibold sm:hidden"
            onClick={onOpenMenu}
          >
            <span className="flex items-center gap-2">
              <List className="size-4" />
              {t('settings.models.order.openMenu')}
            </span>
            <ChevronRight className="size-4 text-muted" />
          </Button>
        )}
      </div>
    </MenuDrawer>
  );
};

/** 「<name> 没跑通」, with the server's reason when it gave one. */
const failedLine = (t: TFunction, name: string, detail: string | null): string =>
  detail
    ? (t('settings.models.dryRun.failed', { name, detail }) as string)
    : (t('settings.models.dryRun.failedUnknown', { name }) as string);

/**
 * 试跑 — one real turn through this Agent's chain, and its answer.
 *
 * Full-width at every breakpoint, at the end of the list. No V6 or M02 frame
 * draws it, so it takes the geometry of the phone's 模型菜单与映射 row rather than
 * inventing one, and it is a row instead of a third footer button because the
 * answer belongs UNDER the question: 「这条链现在能不能跑通」 is about the list
 * above it, and a footer control would report on it from the wrong end of the
 * sheet, with nowhere to put the result line.
 *
 * It reports the head the SERVER picked, never one this drawer chose: the probe
 * takes no model, the reply names the source it reached, and a null latency is a
 * head the Hub does not carry the request for (a native CLI login) — which is
 * `可用` without a number, not a zero.
 */
const DryRunRow: React.FC<{
  agent: AgentSupply;
  sources: Source[];
  /** The chain as the USER has it — see the reset effect for why the report is
   *  keyed on this and not on the head the server computes from it. */
  chainKey: string;
  /** The drawer's order PUT is in flight. A probe started now would answer for the
   *  chain the server still holds, filed under the one the user already sees —
   *  `dryRunRowView` owns that rule. */
  saving: boolean;
  /** Re-read sources + agents, the drawer's own `onSaved`: a failing 试跑 is a
   *  write, so the page it sits on is stale until this runs — and `probeArrival`
   *  owns the part that is easy to get wrong, that this is owed even for an answer
   *  the row will not draw. */
  reread: () => void;
}> = ({ agent, sources, chainKey, saving, reread }) => {
  const { t } = useTranslation();
  const plan = dryRunPlan(agent);
  const [running, setRunning] = React.useState(false);
  const [outcome, setOutcome] = React.useState<DryRunOutcome | null>(null);
  // The already-resolved sentence, not the key: a thrown failure may carry no key
  // at all, and `null` then has to mean 「nothing ran yet」 rather than 「it failed
  // for a reason nobody named」.
  const [errorText, setErrorText] = React.useState<string | null>(null);
  const seq = React.useRef(0);

  // A result describes ONE chain, and editing the chain stops it being about
  // anything — so it goes, rather than sitting there under a list it no longer
  // answers for.
  //
  // Keyed on the chain the USER edits, not on `agent.current`, because the head
  // is where the two diverge: a failing 试跑 cools its own head down, so the
  // re-read below moves `agent.current` almost every time the report is a
  // failure. Keyed on the head, the refresh would erase the sentence the click
  // produced — and the line names its source, so after a self-inflicted move it
  // is the EXPLANATION for the new head rather than a claim about it.
  React.useEffect(() => {
    seq.current += 1;
    setRunning(false);
    setOutcome(null);
    setErrorText(null);
  }, [chainKey]);

  const run = async (backend: AgentBackend) => {
    const mine = ++seq.current;
    setRunning(true);
    setOutcome(null);
    setErrorText(null);
    try {
      const probe = await modelsApi.probeAgent(backend);
      // `seq` guards the REPORT, not the refetch. An edit to the chain while this
      // was in flight makes the answer moot, but not the cooldown it wrote — and
      // that edit's own refetch went out before this returned, so it cannot have
      // seen it. `probeArrival` owns the split; the verdict is stored first, then
      // the page behind this sheet is corrected.
      const arrival = probeArrival({ kind: 'result', probe }, seq.current === mine);
      if (arrival.report) setOutcome(dryRunOutcome(probe, sources));
      if (arrival.reread) reread();
    } catch (err) {
      const failure = apiFailure(err);
      // `serverNamed` is the error's to state, not this site's to infer from
      // 「is it one of ours?」: a response that would not parse is one of ours and
      // names nothing, and that is the case the reread below exists for. The code
      // rides along for the opposite case — a refusal that named itself and, in
      // naming itself, disproved the head this button was drawn from.
      const arrival = probeArrival(
        {
          kind: 'thrown',
          serverNamed: failure?.serverNamed ?? false,
          code: failure?.code ?? null,
        },
        seq.current === mine,
      );
      // The server's own reason when it named one (`probe_no_candidate` carries a
      // detail key), the generic line when it didn't — the same degradation the
      // rest of the page gives a server-chosen key.
      if (arrival.report) setErrorText(serverText(t, failure?.detail, 'settings.models.dryRun.error'));
      // Either because the probe may have run and written with the answer lost on
      // the way back, or because the refusal that came back contradicts the chain
      // this row is drawn from — `probeArrival` keeps those two apart.
      if (arrival.reread) reread();
    } finally {
      if (seq.current === mine) setRunning(false);
    }
  };

  const ok = outcome?.kind === 'ok';
  const line = !outcome
    ? errorText
    : outcome.kind === 'ok'
      ? outcome.latencyMs !== null
        ? (t('settings.models.dryRun.ok', { name: outcome.sourceName, ms: outcome.latencyMs }) as string)
        : (t('settings.models.dryRun.okNoLatency', { name: outcome.sourceName }) as string)
      : // The detail is the server's key; with none, the name alone is the honest
        // sentence rather than a machine code appended to it.
        failedLine(t, outcome.sourceName, serverText(t, outcome.detailKey));

  // What this row IS right now, which the plan alone cannot say: no head means no
  // control (Direct mode, or waiting/interrupted — the page states that one level
  // up with the remedy attached, so a disabled 试跑 would only repeat it), and yet
  // this very row is how a chain LOSES its head, because a failing probe cools its
  // own head down. The answer therefore outlives the chain it was about; only the
  // control goes with it. `saving` is in there for the mirror-image reason — see
  // `dryRunRowView`.
  const { backend, enabled, report } = dryRunRowView(plan, { line, saving, running });
  if (backend === null && !report) return null;

  return (
    <div className="mt-1 flex flex-col gap-2">
      {backend !== null && (
        <Button
          variant="outline"
          className="h-[46px] w-full justify-between rounded-xl px-4 text-[13px] font-semibold"
          onClick={() => void run(backend)}
          disabled={!enabled}
        >
          <span className="flex items-center gap-2">
            {running ? <Loader2 className="size-4 animate-spin" /> : <Zap className="size-4" />}
            {t(running ? 'settings.models.dryRun.running' : 'settings.models.dryRun.action')}
          </span>
        </Button>
      )}
      {report && (
        <p
          className={cn(
            'flex items-start gap-1.5 px-1 text-[12px] leading-relaxed',
            ok ? 'text-mint' : 'text-gold',
          )}
        >
          {ok ? (
            <CheckCircle2 className="mt-[2px] size-3.5 shrink-0" />
          ) : (
            <TriangleAlert className="mt-[2px] size-3.5 shrink-0" />
          )}
          {line}
        </p>
      )}
    </div>
  );
};

const EnabledRow: React.FC<{
  source: Source;
  index: number;
  isCurrent: boolean;
  subline: string;
  busy: boolean;
  /** Drag ended — persist whatever order the list settled into. */
  onCommit: () => void;
  /** Keyboard path beside drag; the grip is a real button, not a decoration. */
  onMove: (delta: -1 | 1) => void;
  onRemove: () => void;
}> = ({ source, index, isCurrent, subline, busy, onCommit, onMove, onRemove }) => {
  const { t } = useTranslation();
  const controls = useDragControls();

  return (
    <Reorder.Item
      value={source.id}
      dragListener={false}
      dragControls={controls}
      onDragEnd={onCommit}
      className={cn(
        ROW,
        'list-none',
        isCurrent ? 'border-mint/35 bg-mint/[0.03]' : 'border-border',
      )}
    >
      {/* Disabled while a PUT is in flight, like every other control in this
          drawer — and for a sharper reason than symmetry. Both reorder paths land
          in the same `persist`, which holds ONE `saved.current` and captures its
          own rollback: two overlapping writes let the older echo replace the
          newer order, or a late failure roll back past a write that succeeded.
          One handle carries both paths, so gating it closes both. */}
      <button
        type="button"
        aria-label={t('settings.models.source.reorder') as string}
        aria-keyshortcuts="ArrowUp ArrowDown"
        disabled={busy}
        onPointerDown={(e) => controls.start(e)}
        onKeyDown={(e) => {
          if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
          e.preventDefault();
          onMove(e.key === 'ArrowUp' ? -1 : 1);
        }}
        className="relative flex size-4 shrink-0 cursor-grab touch-none items-center justify-center text-muted/55 transition-colors hover:text-muted active:cursor-grabbing disabled:cursor-default disabled:opacity-50"
      >
        <GripVertical className="size-4" />
        {/* A 16px handle is unhittable with a thumb. A real child box (not a
            pseudo-element, which takes no pointer events) grows the target to
            40×40 on phones without moving the glyph off the frame's column. */}
        <span className="absolute -inset-3 sm:hidden" aria-hidden />
      </button>

      <span className={NUMBER}>{index + 1}</span>
      <SourceTile source={source} />
      <Identity name={source.display_name} subline={subline} amber={needsAttention(source.state)} />

      {isCurrent ? <CurrentChip /> : <StateChip state={source.state} />}

      {/* 取消启用 is one of the two things this drawer exists for, so it can't be
          desktop-only — M02 drew the row without a ✕, but a frame omitting a
          control is not the product deciding phones may not use it. The mis-tap
          worry is answered where mis-taps happen: an enlarged hit box that stops
          well clear of the grip at the row's other end, and an action that only
          moves the source down to 未启用 (re-add is one tap away). */}
      <button
        type="button"
        aria-label={t('settings.models.order.disable') as string}
        onClick={onRemove}
        disabled={busy}
        className="relative flex size-6 shrink-0 items-center justify-center rounded-md text-muted/60 transition-colors hover:bg-surface-2 hover:text-foreground disabled:opacity-50"
      >
        <X className="size-4" />
        <span className="absolute -inset-2 sm:hidden" aria-hidden />
      </button>
    </Reorder.Item>
  );
};

export default SourceOrderDrawer;
