import { useTranslation } from 'react-i18next';

import type { VaultHdAccount, VaultSecret } from '@/context/ApiContext';

/** A seed-phrase wallet: a keypair row whose signer is an HD seed (docs/plans/vault-hd-wallets.md). */
export const isHdWallet = (secret: Pick<VaultSecret, 'signer_kind'>) => secret.signer_kind === 'hd-seed';

/** An account's name, or its localized fallback (the server keeps null for "not named"). */
export function useAccountName() {
  const { t } = useTranslation();
  return (account: Pick<VaultHdAccount, 'account' | 'name'>) => account.name
    || (account.account === 0 ? t('vaults.hd.mainAccount') : t('vaults.hd.accountFallback', { account: account.account }));
}

