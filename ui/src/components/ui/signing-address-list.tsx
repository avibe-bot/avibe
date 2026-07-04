import { useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { SigningAddresses } from '@/context/ApiContext';
import { cn, copyTextToClipboard } from '@/lib/utils';

// Display order: ETH first, then BTC modern → legacy. Each entry maps a backend
// signing_addresses key to a short label; missing entries are skipped.
const ROWS: ReadonlyArray<{ key: keyof SigningAddresses; labelKey: string }> = [
  { key: 'eth', labelKey: 'vaults.addresses.eth' },
  { key: 'btc_segwit', labelKey: 'vaults.addresses.btcSegwit' },
  { key: 'btc_taproot', labelKey: 'vaults.addresses.btcTaproot' },
  { key: 'btc_legacy', labelKey: 'vaults.addresses.btcLegacy' },
];

const AddressRow: React.FC<{ label: string; value: string }> = ({ label, value }) => {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex min-w-0 items-center gap-2">
      <span className="w-[68px] shrink-0 text-[11px] font-medium text-muted">{label}</span>
      <code className="min-w-0 flex-1 truncate rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[11.5px] text-foreground" title={value}>
        {value}
      </code>
      <button
        type="button"
        onClick={() => {
          // Shared helper: falls back to execCommand on LAN-HTTP where navigator.clipboard
          // is unavailable (non-secure context).
          void copyTextToClipboard(value).then((ok) => {
            if (!ok) return;
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          });
        }}
        aria-label={t('vaults.addresses.copy', { label })}
        className="shrink-0 text-muted transition-colors hover:text-foreground"
      >
        {copied ? <Check className="size-3.5 text-mint" /> : <Copy className="size-3.5" />}
      </button>
    </div>
  );
};

/**
 * Compact labeled list of a signing key's derived receive addresses (ETH + BTC
 * variants), each truncated with a copy button. Shared by the vault list row and
 * the create-form key builder. Renders nothing when there are no addresses.
 */
export const SigningAddressList: React.FC<{ addresses?: SigningAddresses | null; className?: string }> = ({
  addresses,
  className,
}) => {
  const { t } = useTranslation();
  if (!addresses) return null;
  const rows = ROWS.filter((row) => addresses[row.key]);
  if (rows.length === 0) return null;
  return (
    <div className={cn('flex min-w-0 flex-col gap-1', className)}>
      {rows.map((row) => (
        <AddressRow key={row.key} label={t(row.labelKey)} value={addresses[row.key] as string} />
      ))}
    </div>
  );
};
