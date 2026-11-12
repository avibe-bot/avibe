import { useTranslation } from 'react-i18next';

import { HintLine } from './vault-key-builder-parts';

/**
 * The signing-key builder of the Add dialog on the protected tier (ui/design: vault/solana/create-protected).
 * The key is born inside the sandbox when the secret is created, so there is nothing to choose here:
 * one line says when the address appears, and the ⓘ says where the key lives. `chain` decides the
 * copy: a protected Bitcoin key carries its spending limits.
 */
export const VaultProtectedKeyFields: React.FC<{ chain: 'evm-bitcoin' | 'solana' }> = ({ chain }) => {
  const { t } = useTranslation();
  const info = chain === 'solana'
    ? t('vaults.dialog.protectedKeyInfo')
    : (
      <span className="flex flex-col gap-2">
        <span>{t('vaults.dialog.protectedKeyInfo')}</span>
        <span>{t('vaults.dialog.protectedBitcoinInfo')}</span>
      </span>
    );
  return <HintLine text={t('vaults.dialog.signingCreateHint')} info={info} />;
};
