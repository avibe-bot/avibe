import { Loader2, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { SigningAddresses } from '@/context/ApiContext';
import { Button } from './button';
import { SigningAddressList } from './signing-address-list';

/**
 * The signing-key builder of the Add dialog on the protected tier (ui/design: vault/solana/create-protected,
 * vault/solana/create-protected-generated). The key is born inside the sandbox, so this offers only
 * Generate, and shows the public addresses the sandbox returned. `chain` decides the copy: a Solana
 * key is an Ed25519 key with one Solana address.
 */
export const VaultProtectedKeyFields: React.FC<{
  chain: 'evm-bitcoin' | 'solana';
  /** The addresses of the key the sandbox generated; `undefined` before the first Generate. */
  generated?: { addresses?: SigningAddresses | null };
  generating: boolean;
  error?: string | null;
  disabled?: boolean;
  onGenerate: () => void;
}> = ({ chain, generated, generating, error, disabled = false, onGenerate }) => {
  const { t } = useTranslation();
  const solana = chain === 'solana';
  return (
    <div className="flex flex-col gap-2.5 rounded-[10px] border border-edge bg-surface-2 px-3 py-3 shadow-elevation-0">
      <span className="text-xs text-muted">{t(solana ? 'vaults.dialog.solanaProtectedHelp' : 'vaults.dialog.signingKeyProtectedHelp')}</span>
      <Button type="button" variant="secondary" disabled={disabled || generating} onClick={onGenerate}>
        {generating ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
        {generated ? t('vaults.dialog.signingRegenerate') : t('vaults.dialog.signingGenerateCta')}
      </Button>
      {generated && (
        <div className="flex min-w-0 flex-col gap-1.5">
          <span className="text-xs font-medium text-muted">{t('vaults.dialog.signingAddresses')}</span>
          {generated.addresses ? (
            <SigningAddressList addresses={generated.addresses} />
          ) : (
            <span className="text-xs text-muted">{t('vaults.dialog.signingAddressesUnavailable')}</span>
          )}
          <span className="text-xs text-muted">{t(solana ? 'vaults.dialog.solanaProtectedBornHint' : 'vaults.dialog.signingKeyProtectedBornHint')}</span>
        </div>
      )}
      {error && <span className="text-xs text-destructive-ink">{error}</span>}
    </div>
  );
};
