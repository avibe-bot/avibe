// A Source model's capability chips (`vibey-hosted-tools.md` §8.2, design.pen
// frame yjGXx): one per hosted tool with a record, and one per reasoning effort
// the Source refused. No record draws nothing, because unused is the normal
// state rather than a problem. Every chip comes from real requests; a refusal
// chip carries the clear that lets the next request try this Source again.
//
// The hover/focus/tap detail follows TimeHint: a phone has no hover, so a tap
// toggles it, and the chip's accessible name carries the same facts.
import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { AlertCircle, AtSign, CheckCircle2, Gauge, Globe, Image as ImageIcon } from 'lucide-react';

import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import { formatDayTime, formatMonthDay, formatSentences } from './format';
import type { ModelCapability } from './hostedTools';
import type { HostedToolName, RefusedEffort } from './types';

const TOOL_ICON: Record<HostedToolName, React.ComponentType<{ className?: string }>> = {
  web_search: Globe,
  web_fetch: Globe,
  x_search: AtSign,
  image_generate: ImageIcon,
};

type Fact = { term: string; value: string; mono?: boolean };

/** The facts a refusal states: when it was learned, why, how long it stays, and the last success if any. */
const useRefusalFacts = () => {
  const { t, i18n } = useTranslation();
  return (refusal: { at: string; reason: string | null; lastOk?: string | null }): Fact[] => {
    const facts: Fact[] = [{
      term: t('settings.models.capability.recorded') as string,
      value: t('settings.models.capability.recordedValue', { time: formatDayTime(refusal.at, i18n.language) }) as string,
    }];
    if (refusal.reason) facts.push({ term: t('settings.models.capability.reason') as string, value: refusal.reason, mono: true });
    facts.push({ term: t('settings.models.capability.kept') as string, value: t('settings.models.capability.keptValue') as string });
    if (refusal.lastOk) {
      facts.push({ term: t('settings.models.capability.lastOk') as string, value: formatMonthDay(refusal.lastOk) });
    }
    return facts;
  };
};

const EvidenceChip: React.FC<{
  data: string;
  label: string;
  explanation: string;
  facts: Fact[];
  hint?: string;
  tone: 'ok' | 'refused';
  TitleIcon: React.ComponentType<{ className?: string }>;
  clear?: { label: string; disabled: boolean; onClick: () => void };
}> = ({ data, label, explanation, facts, hint, tone, TitleIcon, clear }) => {
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
  const Icon = tone === 'ok' ? CheckCircle2 : AlertCircle;
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverAnchor asChild>
        {/* Shrinks inside its column and truncates; the detail always has the whole label. */}
        <span
          data-capability={data}
          className={cn(
            'model-hub-pill model-hub-capability-chip min-w-0 max-w-full flex-initial border',
            tone === 'ok' ? 'model-hub-status-mint' : 'model-hub-status-gold',
          )}
        >
          <button
            ref={anchor}
            type="button"
            aria-label={accessibleName as string}
            aria-haspopup="dialog"
            aria-expanded={open}
            aria-controls={open ? detailId : undefined}
            className="min-w-0"
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
          {clear && (
            <button type="button" className="model-hub-capability-clear shrink-0" aria-label={clear.label} disabled={clear.disabled} onClick={clear.onClick}>
              {t('settings.models.capability.clear')}
            </button>
          )}
        </span>
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
        {hint && <span className="model-hub-capability-explain">{hint}</span>}
      </PopoverContent>
    </Popover>
  );
};

const CapabilityChip: React.FC<{
  capability: ModelCapability;
  modelId: string;
  clearing: boolean;
  onClear?: (tool: HostedToolName) => void;
}> = ({ capability, modelId, clearing, onClear }) => {
  const { t } = useTranslation();
  const refusalFacts = useRefusalFacts();
  const { evidence } = capability;
  const tool = t(`settings.models.tools.tool.${capability.tool}.name`) as string;
  const toolInline = t(`settings.models.tools.tool.${capability.tool}.inline`) as string;
  if (evidence.kind === 'ok') {
    const date = formatMonthDay(evidence.success.at);
    return (
      <EvidenceChip
        data={`${capability.tool}:ok`}
        label={t('settings.models.capability.chip.ok', { tool, date }) as string}
        explanation={t(`settings.models.capability.explain.${evidence.success.mode}`, { toolInline }) as string}
        facts={[]}
        tone="ok"
        TitleIcon={TOOL_ICON[capability.tool]}
      />
    );
  }
  const { refusal } = evidence;
  const state = refusal.aspect === 'call' ? 'refused' : 'replay';
  return (
    <EvidenceChip
      data={`${capability.tool}:${state}`}
      label={t(`settings.models.capability.chip.${state}`, { tool }) as string}
      explanation={t(`settings.models.capability.explain.${state}`, { toolInline }) as string}
      facts={refusalFacts({ at: refusal.at, reason: refusal.reason, lastOk: evidence.lastOk?.at ?? null })}
      hint={onClear ? t('settings.models.capability.clearHint') as string : undefined}
      tone="refused"
      TitleIcon={TOOL_ICON[capability.tool]}
      clear={onClear && {
        label: t('settings.models.capability.clearTool', { tool: toolInline, model: modelId }) as string,
        disabled: clearing,
        onClick: () => onClear(capability.tool),
      }}
    />
  );
};

const EffortChip: React.FC<{ refused: RefusedEffort; modelId: string; clearing: boolean; onClear?: (effort: string) => void }> = ({ refused, modelId, clearing, onClear }) => {
  const { t } = useTranslation();
  const facts = useRefusalFacts()({ at: refused.observed_at, reason: refused.detail });
  const effort = t(`chat.picker.effortOptions.${refused.effort}`, { defaultValue: refused.effort }) as string;
  return (
    <EvidenceChip
      data={`effort:${refused.effort}`}
      label={t('settings.models.capability.effort.unsupported', { effort }) as string}
      explanation={t('settings.models.capability.effort.explain') as string}
      facts={facts}
      hint={onClear ? t('settings.models.capability.clearHint') as string : undefined}
      tone="refused"
      TitleIcon={Gauge}
      clear={onClear && {
        label: t('settings.models.capability.clearEffort', { effort, model: modelId }) as string,
        disabled: clearing,
        onClick: () => onClear(refused.effort),
      }}
    />
  );
};

export const HostedCapabilityChips: React.FC<{
  modelId: string;
  capabilities: readonly ModelCapability[];
  refusedEfforts?: readonly RefusedEffort[];
  /** Tools with a write out: their clears wait for its answer. */
  pendingTools: ReadonlySet<HostedToolName>;
  /** The Source has a write out: effort clears wait for it. */
  sourceBusy: boolean;
  onClearTool?: (tool: HostedToolName) => void;
  onClearEffort?: (effort: string) => void;
}> = ({ modelId, capabilities, refusedEfforts = [], pendingTools, sourceBusy, onClearTool, onClearEffort }) => (
  <span className="flex min-w-0 flex-wrap items-center gap-1.5">
    {capabilities.map((capability) => (
      <CapabilityChip key={`${capability.tool}:${capability.evidence.kind === 'ok' ? 'ok' : capability.evidence.refusal.aspect}`} capability={capability} modelId={modelId} clearing={pendingTools.has(capability.tool)} onClear={onClearTool} />
    ))}
    {refusedEfforts.map((refused) => (
      <EffortChip key={refused.effort} refused={refused} modelId={modelId} clearing={sourceBusy} onClear={onClearEffort} />
    ))}
  </span>
);
