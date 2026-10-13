// A Source model's hosted-tool capability chips (`vibey-hosted-tools.md` §6.2,
// design.pen frame FB4vC). One chip per tool with evidence; no record draws
// nothing, because unchecked is the normal state rather than a problem.
//
// The hover/focus/tap detail follows TimeHint: a phone has no hover, so a tap
// toggles it, and the chip's accessible name carries the same facts.
import * as React from 'react';
import { useTranslation } from 'react-i18next';
import { AtSign, Ban, Check, Globe, Image as ImageIcon, RotateCw, TriangleAlert, Waypoints } from 'lucide-react';

import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import { formatDayTime, formatMonthDay } from './format';
import type { ModelCapability } from './hostedTools';
import type { HostedToolName } from './types';

const TOOL_ICON: Record<HostedToolName, React.ComponentType<{ className?: string }>> = {
  web_search: Globe,
  x_search: AtSign,
  image_generation: ImageIcon,
};

/**
 * The chip's state. An unsupported tool that is on reaches this Source through
 * the proxy; a probe record older than 30 days, either way, asks for a new check.
 */
const chipState = ({ chip, enabled }: ModelCapability) => {
  if (chip.kind === 'engineUpgraded') return 'engineUpgraded' as const;
  if (chip.kind === 'native') return chip.aged ? 'reverify' as const : 'native' as const;
  if (enabled) return chip.aged ? 'viaProxyAged' as const : 'viaProxy' as const;
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

const CapabilityChip: React.FC<{ capability: ModelCapability }> = ({ capability }) => {
  const { t, i18n } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const anchor = React.useRef<HTMLButtonElement>(null);
  const pointer = React.useRef<string | null>(null);
  const state = chipState(capability);
  const tool = t(`settings.models.tools.tool.${capability.tool}.name`) as string;
  const { evidence } = capability;
  const label = t(`settings.models.capability.chip.${state}`, {
    tool,
    date: evidence.observed_at ? formatMonthDay(evidence.observed_at) : '',
  }) as string;
  const explanation = t(`settings.models.capability.explain.${state}`, {
    tool,
    toolInline: t(`settings.models.tools.tool.${capability.tool}.inline`),
  }) as string;
  const recorded = evidence.observed_at
    ? t('settings.models.capability.recordedValue', {
        time: formatDayTime(evidence.observed_at, i18n.language),
        basis: t(`settings.models.capability.basis.${evidence.basis === 'observed' ? 'observed' : 'probe'}`),
      }) as string
    : null;
  const StateIcon = STATE_ICON[state];
  const ToolIcon = TOOL_ICON[capability.tool];
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverAnchor asChild>
        <button
          ref={anchor}
          type="button"
          data-capability={`${capability.tool}:${state}`}
          aria-label={[label, explanation, recorded, evidence.detail].filter(Boolean).join('. ')}
          // Shrinks inside its column and truncates; the detail always has the whole label.
          className={cn('model-hub-pill min-w-0 max-w-full flex-initial gap-1 border', STATE_CLASS[state])}
          onPointerDown={(event) => { pointer.current = event.pointerType; }}
          onPointerEnter={(event) => { if (event.pointerType === 'mouse') setOpen(true); }}
          onPointerLeave={(event) => { if (event.pointerType === 'mouse') setOpen(false); }}
          onFocus={() => { if (pointer.current === null) setOpen(true); }}
          onBlur={() => { pointer.current = null; setOpen(false); }}
          onClick={() => {
            if (pointer.current === 'mouse') setOpen(true);
            else setOpen((previous) => !previous);
          }}
        >
          <StateIcon className="size-3 shrink-0" aria-hidden />
          <span className="truncate">{label}</span>
        </button>
      </PopoverAnchor>
      <PopoverContent
        side="bottom"
        align="start"
        sideOffset={6}
        onOpenAutoFocus={(event) => event.preventDefault()}
        onInteractOutside={(event) => { if (anchor.current?.contains(event.target as Node)) event.preventDefault(); }}
        className="model-hub-capability-detail flex w-[320px] max-w-[calc(100vw-24px)] flex-col gap-2.5 text-foreground"
      >
        <span className="flex items-center gap-1.5 font-bold"><ToolIcon className="model-hub-ink-mint size-3.5" aria-hidden />{label}</span>
        <span className="model-hub-capability-explain">{explanation}</span>
        {(recorded || evidence.detail) && (
          <dl className="model-hub-capability-facts grid gap-x-3 gap-y-1 rounded-lg">
            {recorded && <><dt>{t('settings.models.capability.recorded')}</dt><dd>{recorded}</dd></>}
            {evidence.detail && <><dt>{t('settings.models.capability.reason')}</dt><dd className="font-mono">{evidence.detail}</dd></>}
          </dl>
        )}
      </PopoverContent>
    </Popover>
  );
};

export const HostedCapabilityChips: React.FC<{ capabilities: readonly ModelCapability[] }> = ({ capabilities }) => (
  <span className="flex min-w-0 flex-wrap items-center gap-1.5">
    {capabilities.map((capability) => <CapabilityChip key={capability.tool} capability={capability} />)}
  </span>
);
