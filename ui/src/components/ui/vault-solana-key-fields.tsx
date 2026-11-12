import { useTranslation } from 'react-i18next';

import { Textarea } from './textarea';
import { HintLine, KeySourceTabs, type KeySource } from './vault-key-builder-parts';

export type SolanaKeySource = KeySource;

/**
 * The signing-key builder of the Add dialog for a Solana key (ui/design: vault/solana/create-generate,
 * vault/solana/create-import). avault creates a generated key, so it never reaches this browser; an
 * imported key is pasted here and sealed for avault, which is the only place that checks it. Either
 * way the address is known only once the key is saved, so none is shown here.
 */
export const VaultSolanaKeyFields: React.FC<{
  source: SolanaKeySource;
  onSourceChange: (next: SolanaKeySource) => void;
  importText: string;
  onImportTextChange: (next: string) => void;
  /** avault did not import the pasted text. It gives no reason the form could name. */
  importFailed?: boolean;
  disabled?: boolean;
}> = ({ source, onSourceChange, importText, onImportTextChange, importFailed = false, disabled = false }) => {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-2.5">
      <KeySourceTabs value={source} onChange={onSourceChange} disabled={disabled} />
      {source === 'generate' ? (
        <HintLine text={t('vaults.dialog.signingCreateHint')} info={t('vaults.dialog.solanaKeyInfo')} />
      ) : (
        <>
          <Textarea
            value={importText}
            onChange={(event) => onImportTextChange(event.target.value)}
            rows={4}
            spellCheck={false}
            autoComplete="off"
            disabled={disabled}
            aria-label={t('vaults.dialog.signingImport')}
            placeholder={t('vaults.dialog.solanaImportPlaceholder')}
            className="font-mono text-xs"
          />
          {importFailed ? (
            <span role="alert" className="text-xs text-destructive-ink">{t('vaults.dialog.errors.solanaImportFailed')}</span>
          ) : (
            <HintLine text={t('vaults.dialog.solanaImportHint')} info={t('vaults.dialog.solanaImportInfo')} />
          )}
        </>
      )}
    </div>
  );
};
