// A Source model's capability chips (`vibey-hosted-tools.md` §6.2, design.pen
// frame FB4vC): one per hosted tool with evidence, and one per reasoning effort
// the Source refused. No record draws nothing, because unchecked is the normal
// state rather than a problem.
//
// The hover/focus/tap detail follows TimeHint: a phone has no hover, so a tap
// toggles it, and the chip's accessible name carries the same facts. The detail
// stays open while the pointer is on it, so its check action can be reached.
import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { AtSign, Ban, Check, Gauge, Globe, Image as ImageIcon, Loader2, Radar, RotateCw, TriangleAlert, Waypoints } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import { formatDayTime, formatMonthDay, formatSentences } from './format';
import { probeableTool, probeKey, type ModelCapability, type ProbeTarget } from './hostedTools';
import type { HostedToolName, HostedToolProbeResult, RefusedEffort } from './types';
import { ProbeAnswerNote } from './HostedProbeDialog';
import { useEstimateLabel } from './useEstimateLabel';

const TOOL_ICON: Record<HostedToolName, React.ComponentType<{ className?: string }>> = {
  web_search: Globe,
  x_search: AtSign,
  image_generation: ImageIcon,
};

/**
 * The chip's state. A probe record older than 30 days, either way, asks for a new
 * check. A refused tool reads "via proxy" only while the server binds a proxy
 * executor for it (`bound`); otherwise it is simply not supported here.
 */
const chipState = ({ chip, proxied }: ModelCapability) => {
  if (chip.kind === 'engineUpgraded') return 'engineUpgraded' as const;
  if (chip.kind === 'native') return chip.aged ? 'reverify' as const : 'native' as const;
  if (proxied) return chip.aged ? 'viaProxyAged' as const : 'viaProxy' as const;
  return chip.aged ? 'unsupportedAged' as const : 'unsupported' as const;
};
type ChipState = ReturnType<typeof chipState>;

const STATE_CLASS: Record<ChipState, string> = {
  native: 'model-hub-status-mint',
  reverify: 'model-hub-status-gold',
  engineUpgraded: 'model-hub-status-gold',
  viaProxy: 'model-hub-accent-pill--cyan',
  viaProxyAged: 'model-hub-status-gold',
  unsupported: 'model-hub-fill-0a border-border text-muted',
  unsupportedAged: 'model-hub-status-gold',
};

const STATE_ICON: Record<ChipState, React.ComponentType<{ className?: string }>> = {
  native: Check,
  reverify: RotateCw,
  engineUpgraded: TriangleAlert,
  viaProxy: Waypoints,
  viaProxyAged: RotateCw,
  unsupported: Ban,
  unsupportedAged: RotateCw,
};

type Fact = { term: string; value: string; mono?: boolean };

/** The facts every evidence record states: when and how it was learned, why, and until when. */
const useEvidenceFacts = () => {
  const { t, i18n } = useTranslation();
  return (record: { basis: 'probe' | 'observed' | null; observed_at: string | null; detail: string | null; changes_at: string | null }): Fact[] => {
    const facts: Fact[] = [];
    if (record.observed_at) {
      facts.push({
        term: t('settings.models.capability.recorded') as string,
        value: t('settings.models.capability.recordedValue', {
          time: formatDayTime(record.observed_at, i18n.language),
          basis: t(`settings.models.capability.basis.${record.basis === 'observed' ? 'observed' : 'probe'}`),
        }) as string,
      });
    }
    if (record.detail) facts.push({ term: t('settings.models.capability.reason') as string, value: record.detail, mono: true });
    if (record.changes_at) {
      facts.push({
        term: t('settings.models.capability.expiry') as string,
        value: t(record.basis === 'observed' ? 'settings.models.capability.expiryObserved' : 'settings.models.capability.expiryProbe', {
          date: formatMonthDay(record.changes_at),
        }) as string,
      });
    }
    return facts;
  };
};

const EvidenceChip: React.FC<{
  data: string;
  label: string;
  explanation: string;
  facts: Fact[];
  className: string;
  Icon: React.ComponentType<{ className?: string }>;
  TitleIcon: React.ComponentType<{ className?: string }>;
  action?: React.ReactNode;
}> = ({ data, label, explanation, facts, className, Icon, TitleIcon, action }) => {
  const { t, i18n } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const detailId = React.useId();
  // The whole spoken name is the locale's composition, as the detail shows it.
  const accessibleName = facts.length === 0
    ? t('settings.models.capability.accessibleName', { label, explanation })
    : t('settings.models.capability.accessibleNameFacts', {
        label,
        explanation,
        facts: formatSentences(
          facts.map((fact) => t('settings.models.capability.accessibleFact', { term: fact.term, value: fact.value }) as string),
          i18n.language,
        ),
      });
  const anchor = React.useRef<HTMLButtonElement>(null);
  const pointer = React.useRef<string | null>(null);
  const closing = React.useRef<number | null>(null);
  const keepOpen = () => {
    if (closing.current !== null) window.clearTimeout(closing.current);
    closing.current = null;
    setOpen(true);
  };
  // Leaving the chip for its detail must not close the detail on the way.
  const closeSoon = () => {
    closing.current = window.setTimeout(() => setOpen(false), 120);
  };
  React.useEffect(() => () => { if (closing.current !== null) window.clearTimeout(closing.current); }, []);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverAnchor asChild>
        <button
          ref={anchor}
          type="button"
          data-capability={data}
          aria-label={accessibleName as string}
          // It opens a detail that may hold a paid re-check: say so.
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-controls={open ? detailId : undefined}
          // Shrinks inside its column and truncates; the detail always has the whole label.
          className={cn('model-hub-pill min-w-0 max-w-full flex-initial gap-1 border', className)}
          onPointerDown={(event) => { pointer.current = event.pointerType; }}
          onPointerEnter={(event) => { if (event.pointerType === 'mouse') keepOpen(); }}
          onPointerLeave={(event) => { if (event.pointerType === 'mouse') closeSoon(); }}
          onFocus={() => { if (pointer.current === null) keepOpen(); }}
          onBlur={() => { pointer.current = null; closeSoon(); }}
          onClick={() => {
            if (pointer.current === 'mouse') setOpen(true);
            else setOpen((previous) => !previous);
          }}
        >
          <Icon className="size-3 shrink-0" aria-hidden />
          <span className="truncate">{label}</span>
        </button>
      </PopoverAnchor>
      <PopoverContent
        id={detailId}
        side="bottom"
        align="start"
        sideOffset={6}
        onOpenAutoFocus={(event) => event.preventDefault()}
        onInteractOutside={(event) => { if (anchor.current?.contains(event.target as Node)) event.preventDefault(); }}
        onPointerEnter={(event) => { if (event.pointerType === 'mouse') keepOpen(); }}
        onPointerLeave={(event) => { if (event.pointerType === 'mouse') closeSoon(); }}
        onFocusCapture={keepOpen}
        className="model-hub-capability-detail flex w-[320px] max-w-[calc(100vw-24px)] flex-col gap-2.5 text-foreground"
      >
        <span className="flex items-center gap-1.5 font-bold"><TitleIcon className="model-hub-ink-mint size-3.5" aria-hidden />{label}</span>
        <span className="model-hub-capability-explain">{explanation}</span>
        {facts.length > 0 && (
          <dl className="model-hub-capability-facts grid gap-x-3 gap-y-1 rounded-lg">
            {facts.map((fact) => (
              <React.Fragment key={fact.term}><dt>{fact.term}</dt><dd className={cn(fact.mono && 'font-mono')}>{fact.value}</dd></React.Fragment>
            ))}
          </dl>
        )}
        {action}
      </PopoverContent>
    </Popover>
  );
};

const CapabilityChip: React.FC<{
  capability: ModelCapability;
  probing: ReadonlySet<string>;
  onProbe?: (target: ProbeTarget) => Promise<HostedToolProbeResult | null>;
}> = ({ capability, probing, onProbe }) => {
  const { t } = useTranslation();
  const facts = useEvidenceFacts()(capability.evidence);
  const estimate = useEstimateLabel()(capability.candidate.probe_estimate);
  const state = chipState(capability);
  const tool = t(`settings.models.tools.tool.${capability.tool}.name`) as string;
  const target: ProbeTarget = { tool: capability.tool, candidate: capability.candidate };
  const running = probing.has(probeKey(target));
  // The last answer from this chip's check, so one that became no evidence still answers here.
  const [last, setLast] = React.useState<HostedToolProbeResult | null | undefined>(undefined);
  const label = t(`settings.models.capability.chip.${state}`, {
    tool,
    date: capability.evidence.observed_at ? formatMonthDay(capability.evidence.observed_at) : '',
  }) as string;
  const explanation = t(`settings.models.capability.explain.${state}`, {
    tool,
    toolInline: t(`settings.models.tools.tool.${capability.tool}.inline`),
  }) as string;
  const action = onProbe && probeableTool(capability.tool)
    ? (
        <span className="flex flex-col items-end gap-1">
          {last !== undefined && <ProbeAnswerNote result={last} />}
          <Button variant="outline" size="xs" disabled={running || probing.size > 0} aria-busy={running} onClick={() => void onProbe(target).then(setLast)}>
            {running ? <Loader2 className="animate-spin" aria-hidden /> : <Radar aria-hidden />}
            {t('settings.models.capability.recheckCost', { cost: estimate ?? t('settings.models.tools.probe.unknown') })}
          </Button>
        </span>
      )
    : undefined;
  return (
    <EvidenceChip
      data={`${capability.tool}:${state}`}
      label={label}
      explanation={explanation}
      facts={facts}
      className={STATE_CLASS[state]}
      Icon={STATE_ICON[state]}
      TitleIcon={TOOL_ICON[capability.tool]}
      action={action}
    />
  );
};

const EffortChip: React.FC<{ refused: RefusedEffort }> = ({ refused }) => {
  const { t } = useTranslation();
  const facts = useEvidenceFacts()(refused);
  const effort = t(`chat.picker.effortOptions.${refused.effort}`, { defaultValue: refused.effort }) as string;
  return (
    <EvidenceChip
      data={`effort:${refused.effort}`}
      label={t('settings.models.capability.effort.unsupported', { effort }) as string}
      explanation={t('settings.models.capability.effort.explain') as string}
      facts={facts}
      className="model-hub-fill-0a border-border text-muted"
      Icon={Ban}
      TitleIcon={Gauge}
    />
  );
};

export const HostedCapabilityChips: React.FC<{
  capabilities: readonly ModelCapability[];
  refusedEfforts?: readonly RefusedEffort[];
  probing: ReadonlySet<string>;
  onProbe?: (target: ProbeTarget) => Promise<HostedToolProbeResult | null>;
}> = ({ capabilities, refusedEfforts = [], probing, onProbe }) => (
  <span className="flex min-w-0 flex-wrap items-center gap-1.5">
    {capabilities.map((capability) => <CapabilityChip key={capability.tool} capability={capability} probing={probing} onProbe={onProbe} />)}
    {refusedEfforts.map((refused) => <EffortChip key={refused.effort} refused={refused} />)}
  </span>
);
