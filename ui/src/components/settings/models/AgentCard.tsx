// The Agent band (design.pen 「产品改造 V6 01」 / 「V6 04」, mobile 「V6 M01」): one
// row per backend — identity + menu-kind badge, then the whole of this backend's
// supply on one line (the model it runs · the numbered source chain it walks ·
// whether that chain is recommended or hand-picked), the supply-mode pill, and
// 来源顺序 / 接入中枢.
//
// V6 replaced the old `model ｜ ● source` composite pill with the chain, because
// the pill could only ever say what is serving RIGHT NOW — the question the page
// actually has to answer is what happens when that stops working. The chain shows
// the fallback path, marks the position the resolver is on, and dims what it has
// already walked past, so the V6 04 failover moment reads off the row directly.
//
// Ordering itself is not editable here: it belongs to the 来源顺序 drawer, which
// only exists for Hub-mode backends (AC-7 — a Direct backend gets no chain and no
// drawer, since the Hub supplies nothing for it).
import * as React from 'react';
import { Link } from 'react-router-dom';
import { ArrowDownUp, ArrowRight, ChevronRight, Download, TriangleAlert, Zap } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { ModeChip } from './chips';
import { ACCENT_ICON, ACCENT_TILE, backendVisual } from './vendorMeta';
import { formatNameList, friendlyModelName } from './format';
import { attribution, chainChips, hasAttribution, type ChainChip } from './supply';
import type { AgentSupply, Source } from './types';

/** 菜单固定 / 菜单开放 — desktop only (M01 drops it: at 360px the badge and the
 *  mode pill fought over the name line, and menu shape is a detail you go looking
 *  for, not something you scan a phone list for). */
const MenuKindBadge: React.FC<{ agent: AgentSupply }> = ({ agent }) => {
  const { t } = useTranslation();
  return (
    <Badge
      variant="secondary"
      className="hidden shrink-0 rounded-md bg-foreground/[0.02] px-[7px] py-0.5 text-[10.5px] font-medium sm:inline-flex"
    >
      {t(`settings.models.agents.menuKind.${agent.menu_kind === 'open' ? 'open' : 'fixed'}`)}
    </Badge>
  );
};

/**
 * The model the next turn asks for. Border + inner tint are one element here: the
 * frame's inner fill spans the box exactly, so a second node would only add a
 * rounding seam.
 *
 * An open-menu backend has no single model — its named Agents each pick their own
 * — so the box counts the ticked menu instead of inventing a headline model.
 */
const ModelBox: React.FC<{ agent: AgentSupply; sources: Source[] }> = ({ agent, sources }) => {
  const { t } = useTranslation();
  const label =
    agent.menu_kind === 'open' && agent.menu
      ? (t('settings.models.agents.modelCount', { count: agent.menu.checked.length }) as string)
      : friendlyModelName(agent, sources);
  if (!label) return null;
  return (
    <span className="inline-flex min-w-0 shrink-0 rounded-lg border border-border bg-foreground/[0.03] px-[9px] py-1">
      <span className="truncate font-mono text-[11.5px] font-semibold text-foreground">{label}</span>
    </span>
  );
};

const CHIP_TONE: Record<ChainChip['tone'], string> = {
  // 当前 — the position the resolver is on.
  current: 'border-mint/40 bg-mint-soft',
  // Already walked past, and unhealthy: the reason the resolver moved on.
  skipped: 'border-border bg-foreground/[0.02] opacity-75',
  neutral: 'border-border bg-foreground/[0.02]',
};

const Chip: React.FC<{ chip: ChainChip }> = ({ chip }) => (
  <span
    className={cn(
      'inline-flex min-w-0 items-center gap-[3px] rounded-md border px-1.5 py-0.5 sm:gap-[5px] sm:px-2 sm:py-[3px]',
      CHIP_TONE[chip.tone],
    )}
  >
    <span
      className={cn(
        'font-mono text-[10px] font-bold sm:text-[10.5px]',
        chip.tone === 'current' ? 'text-mint' : 'text-muted',
      )}
    >
      {chip.position}
    </span>
    <span
      className={cn(
        'truncate text-[10px] font-semibold sm:text-[11px]',
        chip.tone === 'current' ? 'text-mint' : chip.tone === 'skipped' ? 'text-muted' : 'text-foreground',
      )}
    >
      {chip.label}
    </span>
    {/* Gold dot = this source cannot serve right now. It rides the chip even when
        the resolver has not reached it yet, which is the row's early warning: the
        next failover will skip this position too.

        Muted dot = the same skip for a reason nobody can act on from this page: the
        source is healthy and on the route, this process just cannot sign the
        backend's CLI in with it. A second HUE on the one dot, not a second dot — a
        position is stepped over for one reason at a time, and 10px of chip has no
        room to argue. Which is also why neither dot carries its own label: the gold
        one is named by the source row's state chip and the muted one by
        `order.nativeUnavailable` in the order drawer, the two places the user goes
        to do something about it. */}
    {(chip.unhealthy || chip.unavailable) && (
      <span
        className={cn('size-1 shrink-0 rounded-full sm:size-[5px]', chip.unhealthy ? 'bg-gold' : 'bg-muted')}
        aria-hidden
      />
    )}
  </span>
);

/** The numbered fallback path: 1 → 2 → 3, in this backend's own order. Wraps on a
 *  phone (M01 gives it its own full-width line); one line on desktop, where the
 *  frame reads left-to-right as a single path and a mid-chain break would suggest
 *  two of them — long names truncate inside their chip instead. */
const SupplyChain: React.FC<{ chips: ChainChip[] }> = ({ chips }) => (
  <span className="flex min-w-0 flex-wrap items-center gap-[3px] sm:flex-nowrap sm:gap-2">
    {chips.map((chip, i) => (
      <React.Fragment key={chip.sourceId}>
        {i > 0 && <ChevronRight className="size-[9px] shrink-0 text-muted opacity-50 sm:size-[11px]" aria-hidden />}
        <Chip chip={chip} />
      </React.Fragment>
    ))}
  </span>
);

/** 跟随推荐 (cyan) / 自定义 (neutral) — whether the chain above is the server's
 *  recommendation, which absorbs newly added sources, or a frozen hand-picked
 *  subset the server will never touch. */
const PolicyBadge: React.FC<{ agent: AgentSupply; className?: string }> = ({ agent, className }) => {
  const { t } = useTranslation();
  const follow = agent.sources?.policy === 'follow';
  return (
    <Badge
      variant={follow ? 'info' : 'secondary'}
      className={cn(
        'shrink-0 px-2 py-[3px] text-[10px] font-semibold sm:px-[9px] sm:text-[10.5px]',
        !follow && 'bg-foreground/[0.02]',
        className,
      )}
    >
      {t(`settings.models.agents.policy.${follow ? 'follow' : 'custom'}`)}
    </Badge>
  );
};

/**
 * AC-9: name WHO a supply problem is about, from the server's per-Agent
 * projection.
 *
 * Deliberately one line under the chain rather than a badge on it: a failure can
 * hit a named Agent, or only a ticked-but-unassigned menu model, and those two
 * cannot share a slot. A ticked model nobody runs is named WITHOUT an Agent —
 * saying 「Agent X 受影响」 there would be false, which is exactly the half of
 * AC-9 that a per-backend rollup gets wrong.
 */
const AttributionLine: React.FC<{ agent: AgentSupply }> = ({ agent }) => {
  const { t, i18n } = useTranslation();
  const a = attribution(agent);
  if (!hasAttribution(a)) return null;
  // Names come from the payload, so the punctuation between them has to come from
  // the reader's locale rather than from this file — see `formatNameList`.
  const list = (names: string[]) => formatNameList(names, i18n.language);
  const parts: string[] = [];
  if (a.interrupted.length > 0) {
    parts.push(t('settings.models.agents.attribution.interrupted', { names: list(a.interrupted) }) as string);
  }
  if (a.waiting.length > 0) {
    parts.push(t('settings.models.agents.attribution.waiting', { names: list(a.waiting) }) as string);
  }
  if (a.unassignedModels.length > 0) {
    parts.push(t('settings.models.agents.attribution.unassigned', { models: list(a.unassignedModels) }) as string);
  }
  return <span className="text-[11px] leading-relaxed text-gold sm:text-[11.5px]">{parts.join(' · ')}</span>;
};

const AgentRow: React.FC<{
  agent: AgentSupply;
  sources: Source[];
  onConnectHub: (agent: AgentSupply) => void;
  onOpenOrder: (agent: AgentSupply) => void;
  connecting: boolean;
}> = ({ agent, sources, onConnectHub, onOpenOrder, connecting }) => {
  const { t } = useTranslation();
  const { Icon, accent } = backendVisual(agent.backend);
  const hub = agent.mode === 'hub';
  const chips = chainChips(agent, sources);

  const action = hub ? (
    <Button
      variant="secondary"
      className="h-[41px] w-full gap-1.5 rounded-[10px] bg-foreground/[0.02] text-[13px] font-semibold text-foreground sm:h-[31px] sm:w-28 sm:gap-[5px] sm:text-[12px]"
      onClick={() => onOpenOrder(agent)}
    >
      <ArrowDownUp className="size-[15px] text-muted sm:hidden" />
      {t('settings.models.agents.sourceOrder')}
      <ChevronRight className="hidden size-[13px] text-muted sm:inline-block" />
    </Button>
  ) : (
    <Button
      variant="secondary"
      className="h-[41px] w-full gap-1.5 rounded-[10px] border-mint/40 bg-mint-soft text-[13px] font-semibold text-mint hover:bg-mint-soft/70 sm:h-[31px] sm:w-28 sm:gap-[5px] sm:text-[12px]"
      onClick={() => onConnectHub(agent)}
      disabled={connecting}
    >
      {/* The frames use two glyphs for one action: `zap` on mobile, where it
          echoes the 中枢 Hub pill it is offering to become, and `download` on
          desktop, where the pill is already visible in its own column. */}
      <Zap className="size-[15px] sm:hidden" />
      <Download className="hidden size-[13px] sm:inline-block" />
      {t('settings.models.agents.connectHub')}
    </Button>
  );

  return (
    // A grid, not nested flex rows: the frames disagree about the supply block's
    // parent — desktop nests it under the name (indented past the icon tile),
    // mobile spans it across the full row width — and flex cannot re-parent
    // between breakpoints. Grid places the same single element in both, so there
    // is no double mount and no duplicated a11y tree.
    //
    //   mobile   [tile] [name ......] [mode]     desktop  [tile] [name .....] [mode] [action]
    //            [supply ...........  ......]             [tile] [supply ...] [mode] [action]
    //            [action ...........  ......]
    <div
      className={cn(
        'grid grid-cols-[34px_minmax(0,1fr)_auto] items-center gap-x-2.5 gap-y-2.5 border-b border-border px-3.5 py-3.5 last:border-b-0',
        'sm:grid-cols-[34px_minmax(0,1fr)_auto_auto] sm:gap-x-3.5 sm:gap-y-1.5 sm:px-5 sm:py-[13px]',
      )}
    >
      <span
        className={cn(
          'col-start-1 row-start-1 flex size-[34px] items-center justify-center rounded-[10px] sm:row-span-2 sm:self-center',
          ACCENT_TILE[accent],
        )}
      >
        <Icon className={cn('size-4', ACCENT_ICON[accent])} />
      </span>

      <span className="col-start-2 row-start-1 flex min-w-0 items-center gap-2">
        <span className="truncate text-[14px] font-bold text-foreground sm:text-[13.5px] sm:font-semibold">
          {t(`settings.models.backends.${agent.backend}`, { defaultValue: agent.backend })}
        </span>
        <MenuKindBadge agent={agent} />
      </span>

      <span className="col-start-3 row-start-1 justify-self-end sm:row-span-2 sm:ml-2 sm:self-center">
        <ModeChip mode={agent.mode} />
      </span>

      {/* Supply — row 2, full-width on mobile, under the name on desktop. Direct
          mode has no chain to draw, so it says why instead (mobile only: the
          desktop row is a single line in the frame, where a sentence has nowhere
          to sit beside the 接入中枢 button). */}
      <span className="col-span-3 row-start-2 flex min-w-0 flex-col gap-2 sm:col-span-1 sm:col-start-2 sm:row-start-2 sm:gap-1">
        {hub ? (
          <>
            {/* One line in the frame: 模型 · 链路 · 策略. Mobile (M01) breaks it
                after 策略 and gives the chain its own line, so the badge takes an
                explicit order rather than trailing the chain into the wrap. */}
            <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-2 sm:flex-nowrap">
              <ModelBox agent={agent} sources={sources} />
              <PolicyBadge agent={agent} className="order-2 sm:order-3" />
              {chips.length > 0 ? (
                <span className="order-3 flex w-full min-w-0 items-center sm:order-2 sm:w-auto">
                  <SupplyChain chips={chips} />
                </span>
              ) : (
                // Hub is selected but this backend has no enabled source at all.
                // There is no silent Direct fallback to reassure anyone with:
                // `model_hub.resolve()` only returns a Direct launch when the
                // backend's own `mode` is direct, so here it hits `source is None`
                // and raises `mapping_target_unavailable` — the turn fails. Say
                // that, in the same word the rollup uses (供给中断 / interrupted).
                <span className="order-3 flex w-full items-center gap-1.5 text-[11.5px] font-medium text-gold sm:order-2 sm:w-auto">
                  <TriangleAlert className="size-3.5 shrink-0" />
                  {t('settings.models.agents.hubNoSupply')}
                </span>
              )}
            </span>
            <AttributionLine agent={agent} />
          </>
        ) : (
          <span className="text-[11.5px] leading-relaxed text-muted sm:hidden">
            {t('settings.models.agents.directNote')}
          </span>
        )}
      </span>

      <span className="col-span-3 row-start-3 sm:col-span-1 sm:col-start-4 sm:row-start-1 sm:row-span-2 sm:self-center">
        {action}
      </span>
    </div>
  );
};

export const AgentCard: React.FC<{
  agents: AgentSupply[];
  sources: Source[];
  onConnectHub: (agent: AgentSupply) => void;
  onOpenOrder: (agent: AgentSupply) => void;
  connectingBackend: string | null;
}> = ({ agents, sources, onConnectHub, onOpenOrder, connectingBackend }) => {
  const { t } = useTranslation();
  return (
    <section className="rounded-xl border border-border bg-background">
      <div className="flex items-start justify-between gap-4 border-b border-border px-3.5 py-3.5 sm:px-5 sm:py-4">
        <div className="flex min-w-0 flex-col gap-[3px]">
          <h2 className="text-[15px] font-bold text-foreground">{t('settings.models.agents.title')}</h2>
          <p className="text-[11.5px] leading-relaxed text-muted sm:text-[12px]">
            {t('settings.models.agents.subtitle')}
          </p>
        </div>
        <Link
          to="/admin/settings/backends"
          className="inline-flex min-h-10 shrink-0 items-center gap-1 text-[12px] font-semibold text-cyan transition-colors hover:text-cyan/80 sm:min-h-0 sm:text-[12.5px]"
        >
          {t('settings.models.agents.backendSettings')}
          <ArrowRight className="size-3.5" />
        </Link>
      </div>
      <div className="flex flex-col">
        {agents.map((agent) => (
          <AgentRow
            key={agent.backend}
            agent={agent}
            sources={sources}
            onConnectHub={onConnectHub}
            onOpenOrder={onOpenOrder}
            connecting={connectingBackend === agent.backend}
          />
        ))}
      </div>
    </section>
  );
};
