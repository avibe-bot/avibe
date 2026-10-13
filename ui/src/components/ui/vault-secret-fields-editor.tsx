import { useEffect, useState } from 'react';
import { Loader2, Plus, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useApi, type VaultSecret } from '@/context/ApiContext';
import { sealBlindBox, standardCreateBlindBoxContext } from '@/lib/vaultCrypto';
import { useProtectedVault } from '@/lib/useProtectedVault';
import { openVaultsInBrowser } from '@/lib/vaultBrowserHandoff';
import { MAX_VAULT_FIELDS, VAULT_FIELD_PATTERN, vaultDefaultField, vaultFieldNames, vaultRecordName } from '@/lib/vaultFields';
import { Badge } from './badge';
import { Button } from './button';
import { ConfirmDialog } from './confirm-dialog';
import { Input } from './input';
import { VaultProtectedUnlock } from './vault-protected-unlock';

/** Each field mutation returns the next metadata version for the following browser envelope. */
export function VaultSecretFieldsEditor({
  secret, disabled, onBusyChange,
}: {
  secret: VaultSecret;
  disabled: boolean;
  onBusyChange: (busy: boolean) => void;
}) {
  const { t } = useTranslation();
  const api = useApi();
  const vault = useProtectedVault();
  const { refresh } = vault;
  const [current, setCurrent] = useState(secret);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [defaultLabel, setDefaultLabel] = useState(vaultDefaultField(secret));
  const [newField, setNewField] = useState<string | null>(null);
  const [newValue, setNewValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [removeField, setRemoveField] = useState<string | null>(null);
  const fields = vaultFieldNames(current);
  const defaultField = vaultDefaultField(current);
  const isProtected = current.protection === 'protected';
  const locked = disabled || busy;
  const canWriteValue = !isProtected || vault.status === 'unlocked';

  useEffect(() => {
    if (isProtected) void refresh();
  }, [isProtected, refresh]);

  const mutate = async (operation: () => Promise<{ ok: boolean; secret?: VaultSecret; message?: string; code?: string }>) => {
    if (locked) return;
    setBusy(true);
    onBusyChange(true);
    setError(null);
    setSaved(false);
    try {
      const result = await operation();
      if (!result.ok || !result.secret) throw new Error(result.message || result.code || t('vaults.dialog.errors.updateFailed'));
      const next = result.secret;
      const nextDefault = vaultDefaultField(next);
      setCurrent(next);
      setDefaultLabel(nextDefault);
      setDrafts((values) => {
        const remaining = { ...values };
        if (nextDefault !== defaultField) {
          remaining[nextDefault] = remaining[defaultField] ?? '';
          delete remaining[defaultField];
        }
        const names = new Set(vaultFieldNames(next));
        for (const field of Object.keys(remaining)) {
          if (!names.has(field)) delete remaining[field];
        }
        return remaining;
      });
      setSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
      onBusyChange(false);
    }
  };

  const saveValue = (field: string, value: string, adding = false) => void mutate(async () => {
    const record = vaultRecordName(current.name, field, defaultField);
    const clear = () => adding ? setNewValue('') : setDrafts((values) => ({ ...values, [field]: '' }));
    const cryptoFields = isProtected
      ? { sealed: (await vault.sealValue(record, 'static', { valueRef: { current: value }, clear })).envelope }
      : { blind_box: await sealBlindBox(value, await api.getVaultPubkey(), standardCreateBlindBoxContext(record)) };
    const result = await api.updateVaultSecretField(current.name, field, {
      ...cryptoFields, expected_updated_at: current.updated_at,
    }, { handleError: false });
    if (result.ok) {
      clear();
      if (adding) setNewField(null);
    }
    return result;
  });

  return (
    <div className="flex flex-col gap-3" onKeyDown={(event) => {
      // These inputs save through their field buttons, not the enclosing metadata form.
      if (event.key === 'Enter' && event.target instanceof HTMLInputElement) event.preventDefault();
    }}>
      <span className="text-[13px] font-medium">{t('vaults.fields.title')}</span>
      <p className="text-[11px] text-muted-foreground">{t('vaults.fields.editHelp')}</p>
      {fields.map((field) => (
        <div key={field} className="flex flex-col gap-2 rounded-lg border border-border p-3">
          <div className="flex items-center gap-2">
            {field === defaultField ? (
              <>
                <Input value={defaultLabel} disabled={locked}
                  aria-label={t('vaults.fields.defaultName')}
                  onChange={(event) => setDefaultLabel(event.target.value)}
                  className="min-w-0 font-mono" />
                <Badge variant="secondary">{t('vaults.fields.default')}</Badge>
                <Button type="button" size="sm" variant="ghost"
                  disabled={locked || defaultLabel === defaultField || !VAULT_FIELD_PATTERN.test(defaultLabel) || fields.includes(defaultLabel)}
                  onClick={() => void mutate(() => api.updateVaultSecret(current.name, { default_field: defaultLabel }, { handleError: false }))}>
                  {t('vaults.fields.rename')}
                </Button>
              </>
            ) : (
              <>
                <span className="min-w-0 flex-1 break-all font-mono text-xs">{field}</span>
                <Button type="button" size="icon" variant="ghost" disabled={locked}
                  aria-label={t('vaults.fields.remove', { field })} onClick={() => setRemoveField(field)}>
                  <X className="size-4" />
                </Button>
              </>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Input type="password" value={drafts[field] ?? ''} autoComplete="off" disabled={locked}
              aria-label={t('vaults.fields.value', { field })}
              placeholder={t('vaults.fields.replacePlaceholder')}
              onChange={(event) => setDrafts((values) => ({ ...values, [field]: event.target.value }))} />
            <Button type="button" variant="secondary" size="sm"
              aria-label={t('vaults.fields.saveNamed', { field })}
              disabled={locked || !canWriteValue || !drafts[field]}
              onClick={() => saveValue(field, drafts[field])}>{t('vaults.fields.save')}</Button>
          </div>
        </div>
      ))}
      {newField !== null ? (
        <div className="flex flex-col gap-2 rounded-lg border border-border p-3">
          <Input value={newField} disabled={locked} aria-label={t('vaults.fields.newName')}
            placeholder={t('vaults.fields.namePlaceholder')} onChange={(event) => setNewField(event.target.value)} />
          <Input type="password" value={newValue} autoComplete="off" disabled={locked}
            aria-label={t('vaults.fields.newValue')} placeholder={t('vaults.dialog.valuePlaceholder')}
            onChange={(event) => setNewValue(event.target.value)} />
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" size="sm" disabled={locked}
              onClick={() => { setNewField(null); setNewValue(''); }}>{t('vaults.dialog.cancel')}</Button>
            <Button type="button" size="sm"
              disabled={locked || !canWriteValue || !newValue || !VAULT_FIELD_PATTERN.test(newField) || fields.includes(newField)}
              onClick={() => saveValue(newField, newValue, true)}>{t('vaults.fields.save')}</Button>
          </div>
          <span className="text-[11px] text-muted-foreground">{t('vaults.fields.namesHelp')}</span>
        </div>
      ) : (
        <Button type="button" variant="ghost" size="sm" className="self-start" disabled={locked || fields.length >= MAX_VAULT_FIELDS}
          onClick={() => setNewField('')}><Plus className="size-3.5" />{t('vaults.fields.add')}</Button>
      )}
      {isProtected && <VaultProtectedUnlock vault={vault} secretName={current.name}
        onContinueInBrowser={() => openVaultsInBrowser({ kind: 'edit', secretName: current.name })} />}
      {busy && <Loader2 className="size-4 animate-spin text-muted" />}
      {saved && <p role="status" className="text-xs text-mint-ink">{t('vaults.fields.saved')}</p>}
      {error && <p role="alert" className="text-xs text-destructive-ink">{error}</p>}
      <ConfirmDialog open={removeField !== null} onOpenChange={(open) => { if (!open) setRemoveField(null); }}
        title={t('vaults.fields.remove', { field: removeField })}
        description={t('vaults.fields.removeConfirm')}
        confirmLabel={t('vaults.delete')} cancelLabel={t('vaults.dialog.cancel')} destructive
        onConfirm={async () => {
          if (!removeField) return;
          await mutate(() => api.deleteVaultSecretField(current.name, removeField));
          setRemoveField(null);
        }} />
    </div>
  );
}
