// The BTC address-type switch (owner 2026-11-13 14:01: one BTC row with a small switch at its
// label instead of three listed addresses). Two variants for the owner to pick:
//   A  a small segmented pill at the label, the address on the line below;
//   B  a compact chip at the label that opens a short list of the types.
// Only types the key can sign for are offered; with one, there is no switch. Default Taproot.
import { useState } from 'react';
import { Check, ChevronDown } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { SigningAddressRow } from '@/components/ui/signing-address-list';
import { cn } from '@/lib/utils';

import type { BtcAddresses, BtcType } from './data';

const ORDER: BtcType[] = ['taproot', 'segwit', 'legacy'];
const COPY = {
  zh: { taproot: 'Taproot', segwit: 'SegWit', legacy: '传统', taprootHint: 'bc1p 开头', segwitHint: 'bc1q 开头', legacyHint: '1 开头', group: 'BTC 地址类型', btc: 'BTC' },
  en: { taproot: 'Taproot', segwit: 'SegWit', legacy: 'Legacy', taprootHint: 'starts with bc1p', segwitHint: 'starts with bc1q', legacyHint: 'starts with 1', group: 'BTC address type', btc: 'BTC' },
} as const;

const useCopy = () => {
  const { i18n } = useTranslation();
  return COPY[i18n.language?.startsWith('zh') ? 'zh' : 'en'];
};

const offered = (addresses: BtcAddresses) => ORDER.filter((type) => addresses[type]);

/** A: segmented pill next to "BTC"; the address sits on the line below, aligned with the others. */
export function BtcRowSegmented({ addresses, initial = 'taproot', labelClassName = 'w-[68px]' }: {
  addresses: BtcAddresses;
  initial?: BtcType;
  labelClassName?: string;
}) {
  const c = useCopy();
  const types = offered(addresses);
  const [type, setType] = useState<BtcType>(types.includes(initial) ? initial : types[0]);
  if (types.length <= 1) return <SigningAddressRow label={`${c.btc} · ${c[types[0]]}`} value={addresses[types[0]]!} labelClassName={cn(labelClassName, 'whitespace-nowrap')} />;
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <div className="flex min-w-0 items-center gap-2">
        <span className={cn('shrink-0 text-[11px] font-medium text-muted', labelClassName)}>{c.btc}</span>
        <div role="radiogroup" aria-label={c.group} className="inline-flex h-5 items-stretch gap-px rounded-full bg-field p-px">
          {types.map((option) => {
            const active = option === type;
            return (
              <button
                key={option}
                type="button"
                role="radio"
                aria-checked={active}
                onClick={() => setType(option)}
                className={cn(
                  'press rounded-full px-2 text-[10.5px] leading-none outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  active ? 'bg-surface font-semibold text-foreground shadow-elevation-0' : 'font-medium text-muted hover:text-foreground',
                )}
              >
                {c[option]}
              </button>
            );
          })}
        </div>
      </div>
      <SigningAddressRow label="" value={addresses[type]!} labelClassName={labelClassName} />
    </div>
  );
}

/** B: a compact chip at the label ("BTC · Taproot ▾") that opens the offered types. */
export function BtcRowChip({ addresses, initial = 'taproot', labelClassName = 'w-[68px]', open: openInitially = false }: {
  addresses: BtcAddresses;
  initial?: BtcType;
  labelClassName?: string;
  open?: boolean;
}) {
  const c = useCopy();
  const types = offered(addresses);
  const [type, setType] = useState<BtcType>(types.includes(initial) ? initial : types[0]);
  const [open, setOpen] = useState(openInitially);
  if (types.length <= 1) return <SigningAddressRow label={`${c.btc} · ${c[types[0]]}`} value={addresses[types[0]]!} labelClassName={cn(labelClassName, 'whitespace-nowrap')} />;
  const chip = (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`${c.group}: ${c[type]}`}
          className="press -ml-1 inline-flex h-5 items-center gap-0.5 whitespace-nowrap rounded-md px-1 text-[11px] font-medium text-muted outline-none hover:bg-wash-hover hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          {c.btc} · {c[type]}
          <ChevronDown className="size-3" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" sideOffset={4} onOpenAutoFocus={(event) => event.preventDefault()} className="w-56 p-1">
        <div role="listbox" aria-label={c.group} className="flex flex-col">
          {types.map((option) => (
            <button
              key={option}
              type="button"
              role="option"
              aria-selected={option === type}
              onClick={() => { setType(option); setOpen(false); }}
              className="press flex items-center gap-2 rounded-[4px] px-2 py-1.5 text-left text-[12px] text-foreground hover:bg-wash-hover"
            >
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="font-medium">{c[option]}</span>
                <span className="text-[11px] text-muted">{c[`${option}Hint`]}</span>
              </span>
              {option === type ? <Check className="size-3.5 text-mint-ink" /> : null}
            </button>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
  return (
    <div className="flex min-w-0 items-center gap-2">
      <span className={cn('flex shrink-0 items-center', labelClassName)}>{chip}</span>
      <div className="min-w-0 flex-1"><SigningAddressRow label="" value={addresses[type]!} labelClassName="hidden" /></div>
    </div>
  );
}

/** Draft copy in the frame's language. */
export function L({ zh, en }: { zh: string; en: string }) {
  const { i18n } = useTranslation();
  return <>{i18n.language?.startsWith('zh') ? zh : en}</>;
}
