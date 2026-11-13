// ui/design: vault/btc-address-switch/* (owner 2026-11-13 15:09 chose B). One BTC row instead of
// three: the label is a small chip — "BTC" over the shown type — that opens the types this key can
// sign for, each with what its address starts with. One type: a plain label, no chip. The choice
// only changes what is shown; it is not stored or used for signing.
import { useState } from 'react';
import type * as React from 'react';
import { Check, ChevronDown } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { BitcoinAddressType, SigningAddresses } from '@/context/ApiContext';
import { offeredBitcoinTypes, type BitcoinAddresses } from '@/lib/bitcoinAddressTypes';
import { cn } from '@/lib/utils';
import { Popover, PopoverContent, PopoverTrigger } from './popover';
import { SigningAddressRow } from './signing-address-list';


const TYPE_KEY = {
  p2tr: 'vaults.addresses.typeTaproot',
  p2wpkh: 'vaults.addresses.typeSegwit',
  p2pkh: 'vaults.addresses.typeLegacy',
} as const;
const PREFIX_KEY = {
  p2tr: 'vaults.addresses.prefixTaproot',
  p2wpkh: 'vaults.addresses.prefixSegwit',
  p2pkh: 'vaults.addresses.prefixLegacy',
} as const;


export function BtcAddressRow({ types, addresses, labelClassName, singleHint }: {
  /** The server's `bitcoin_address_types`. */
  types: readonly BitcoinAddressType[];
  addresses: BitcoinAddresses;
  labelClassName?: string;
  /** Shown after the type when there is only one (no chip), e.g. how to compare it with a wallet. */
  singleHint?: React.ReactNode;
}) {
  const { t } = useTranslation();
  const offered = offeredBitcoinTypes(types, addresses);
  const [chosen, setChosen] = useState<BitcoinAddressType>('p2tr');
  const [open, setOpen] = useState(false);
  if (offered.length === 0) return null;
  const type = offered.includes(chosen) ? chosen : offered[0];
  const typeLabel = t(TYPE_KEY[type]);
  // "BTC" over the type, so the label fits the column and the address keeps its full width.
  const stacked = (type: React.ReactNode) => (
    <>
      <span className="text-[11px] font-medium leading-tight text-muted">{t('vaults.addresses.btc')}</span>
      <span className="flex items-center gap-0.5 whitespace-nowrap text-[10.5px] font-medium leading-tight text-foreground">{type}</span>
    </>
  );
  if (offered.length === 1) {
    return (
      <div className="flex min-w-0 items-center gap-2">
        <span className={cn('flex w-[68px] shrink-0 flex-col items-start', labelClassName)}>
          {stacked(<>{typeLabel}{singleHint}</>)}
        </span>
        <div className="min-w-0 flex-1">
          <SigningAddressRow label={t('vaults.addresses.btcWithType', { type: typeLabel })} value={addresses[type]!} labelClassName="sr-only" />
        </div>
      </div>
    );
  }
  return (
    <div className="flex min-w-0 items-center gap-2">
      <span className={cn('flex w-[68px] shrink-0', labelClassName)}>
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <button
              type="button"
              aria-label={t('vaults.addresses.btcTypeChoose', { type: typeLabel })}
              className="press -mx-1 flex min-w-0 flex-col items-start rounded-md px-1 py-0.5 text-left outline-none hover:bg-wash-hover focus-visible:ring-2 focus-visible:ring-ring"
            >
              {stacked(<>{typeLabel}<ChevronDown className="size-3 text-muted" /></>)}
            </button>
          </PopoverTrigger>
          <PopoverContent align="start" sideOffset={4} onOpenAutoFocus={(event) => event.preventDefault()} className="w-56 p-1">
            <div role="radiogroup" aria-label={t('vaults.addresses.btcType')} className="flex flex-col">
              {offered.map((option) => (
                <button
                  key={option}
                  type="button"
                  role="radio"
                  aria-checked={option === type}
                  onClick={() => { setChosen(option); setOpen(false); }}
                  className="press flex items-center gap-2 rounded-[4px] px-2 py-1.5 text-left text-[12px] text-foreground outline-none hover:bg-wash-hover focus-visible:bg-wash-hover"
                >
                  <span className="flex min-w-0 flex-1 flex-col">
                    <span className="font-medium">{t(TYPE_KEY[option])}</span>
                    <span className="text-[11px] text-muted">{t(PREFIX_KEY[option])}</span>
                  </span>
                  {option === type ? <Check className="size-3.5 text-mint-ink" /> : null}
                </button>
              ))}
            </div>
          </PopoverContent>
        </Popover>
      </span>
      <div className="min-w-0 flex-1">
        <SigningAddressRow label={t('vaults.addresses.btcWithType', { type: typeLabel })} value={addresses[type]!} labelClassName="sr-only" />
      </div>
    </div>
  );
}

/** A plain signing key's addresses (ui/design: vault/btc-address-switch/b-key-row): ETH, one
 *  Bitcoin row over the types the server says it can sign, Solana. Without a list from the server,
 *  only the Taproot address it already had is shown, never a type inferred from an address. */
export function KeyAddressList({ addresses, types, className }: {
  addresses?: SigningAddresses | null;
  types?: readonly BitcoinAddressType[];
  className?: string;
}) {
  const { t } = useTranslation();
  if (!addresses) return null;
  const bitcoin: BitcoinAddresses = { p2tr: addresses.btc_taproot, p2wpkh: addresses.btc_segwit, p2pkh: addresses.btc_legacy };
  const offered = offeredBitcoinTypes(types ?? ['p2tr'], bitcoin);
  if (!addresses.eth && !addresses.solana && offered.length === 0) return null;
  return (
    <div className={cn('flex min-w-0 flex-col gap-1', className)}>
      {addresses.eth ? <SigningAddressRow label={t('vaults.addresses.eth')} value={addresses.eth} /> : null}
      {offered.length ? <BtcAddressRow types={types ?? ['p2tr']} addresses={bitcoin} /> : null}
      {addresses.solana ? <SigningAddressRow label={t('vaults.addresses.solana')} value={addresses.solana} /> : null}
    </div>
  );
}
