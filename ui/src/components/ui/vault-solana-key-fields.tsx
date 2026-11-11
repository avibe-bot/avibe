import { useTranslation } from 'react-i18next';

import { Button } from './button';
import { Textarea } from './textarea';

export type SolanaKeySource = 'generate' | 'import';

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
    <div className="flex flex-col gap-2.5 rounded-lg border border-edge bg-surface-2 p-3 shadow-elevation-0">
      {/* True of a generated key only: an imported key is pasted here, so this browser reads it. */}
      {source === 'generate' ? <span className="text-xs text-muted">{t('vaults.dialog.solanaGenerateHelp')}</span> : null}
      <div className="grid grid-cols-2 gap-2">
        {(['generate', 'import'] as const).map((src) => (
          <Button
            key={src}
            type="button"
            size="sm"
            variant={source === src ? 'secondary' : 'ghost'}
            aria-pressed={source === src}
            disabled={disabled}
            onClick={() => onSourceChange(src)}
          >
            {src === 'generate' ? t('vaults.dialog.signingGenerate') : t('vaults.dialog.signingImport')}
          </Button>
        ))}
      </div>
      {source === 'generate' ? (
        <span className="text-xs text-muted">{t('vaults.dialog.solanaGenerateHint')}</span>
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
            <span className="text-xs text-muted">{t('vaults.dialog.solanaImportHelp')}</span>
          )}
        </>
      )}
    </div>
  );
};
