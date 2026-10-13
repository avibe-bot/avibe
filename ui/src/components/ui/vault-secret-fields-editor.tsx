import { useEffect, useRef, useState } from 'react';
import { Loader2, Plus, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useApi, type VaultFieldValuePayload, type VaultSecret } from '@/context/ApiContext';
import { sealBlindBox, standardCreateBlindBoxContext } from '@/lib/vaultCrypto';
import { vaultErrorMessage } from '@/lib/vaultErrors';
import { useProtectedVault } from '@/lib/useProtectedVault';
import { openVaultsInBrowser } from '@/lib/vaultBrowserHandoff';
import { MAX_VAULT_FIELDS, VAULT_FIELD_PATTERN, vaultDefaultField, vaultFieldFingerprint, vaultFieldNames, vaultRecordName } from '@/lib/vaultFields';
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
  const currentRef = useRef(secret);
  const stalePropFingerprintRef = useRef<string | null>(null);
  const [drafts, setDrafts] = useState<Map<string, string>>(() => new Map());
  const [defaultLabel, setDefaultLabel] = useState(vaultDefaultField(secret));
  const defaultLabelDirtyRef = useRef(false);
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
    currentRef.current = current;
  }, [current]);

  useEffect(() => {
    const local = currentRef.current;
    if (secret.name !== local.name) {
      currentRef.current = secret;
      stalePropFingerprintRef.current = null;
      defaultLabelDirtyRef.current = false;
      setCurrent(secret);
      setDefaultLabel(vaultDefaultField(secret));
      setDrafts(new Map());
      return;
    }
    const incomingFingerprint = vaultFieldFingerprint(secret);
    if (incomingFingerprint === vaultFieldFingerprint(local)) {
      stalePropFingerprintRef.current = null;
      return;
    }
    if (incomingFingerprint === stalePropFingerprintRef.current) return;
    stalePropFingerprintRef.current = null;
    currentRef.current = secret;
    setCurrent(secret);
    if (!defaultLabelDirtyRef.current) setDefaultLabel(vaultDefaultField(secret));
    setDrafts((values) => {
      const next = new Map(values);
      const oldDefault = vaultDefaultField(local);
      const nextDefault = vaultDefaultField(secret);
      if (oldDefault !== nextDefault && next.has(oldDefault) && !next.has(nextDefault)) {
        next.set(nextDefault, next.get(oldDefault) ?? '');
        next.delete(oldDefault);
      }
      const names = new Set(vaultFieldNames(secret));
      for (const name of next.keys()) {
        if (!names.has(name)) next.delete(name);
      }
      return next;
    });
  }, [secret]);

  useEffect(() => {
    if (isProtected) void refresh();
  }, [isProtected, refresh]);

  const hasErrorCode = (result: unknown, code: string): boolean =>
    typeof result === 'object' && result !== null && 'code' in result && result.code === code;
  const conflict = () => new Error(t('vaults.fields.recordConflict'));
  const adoptSecret = (next: VaultSecret, ownWrite = false) => {
    if (ownWrite && vaultFieldFingerprint(next) !== vaultFieldFingerprint(currentRef.current)) {
      stalePropFingerprintRef.current = vaultFieldFingerprint(currentRef.current);
    }
    currentRef.current = next;
    setCurrent(next);
    defaultLabelDirtyRef.current = false;
    setDefaultLabel(vaultDefaultField(next));
    setDrafts((values) => {
      const remaining = new Map(values);
      const names = new Set(vaultFieldNames(next));
      for (const name of remaining.keys()) {
        if (!names.has(name)) remaining.delete(name);
      }
      return remaining;
    });
  };
  const freshSecret = async (): Promise<VaultSecret> => {
    const result = await api.listVaultSecrets({ cache: false });
    const next = result.secrets.find((entry) => entry.name === current.name);
    if (!next) throw conflict();
    adoptSecret(next);
    return next;
  };

  const mutate = async (operation: () => Promise<{ ok: boolean; secret?: VaultSecret; message?: string; code?: string }>) => {
    if (locked) return;
    setBusy(true);
    onBusyChange(true);
    setError(null);
    setSaved(false);
    try {
      const result = await operation();
      if (!result.ok || !result.secret) {
        throw Object.assign(new Error(vaultErrorMessage(t, result, t('vaults.dialog.errors.updateFailed'))), { code: result.code });
      }
      const next = result.secret;
      adoptSecret(next, true);
      setSaved(true);
    } catch (err) {
      if (hasErrorCode(err, 'secret_conflict')) {
        try {
          await freshSecret();
          setError(t('vaults.fields.secretConflict'));
        } catch (refreshError) {
          setError(vaultErrorMessage(t, refreshError, t('vaults.dialog.errors.updateFailed')));
        }
      } else {
        setError(vaultErrorMessage(t, err, t('vaults.dialog.errors.updateFailed')));
      }
    } finally {
      setBusy(false);
      onBusyChange(false);
    }
  };

  const saveValue = (field: string, value: string, adding = false) => {
    // Keep this one local copy only until the write settles. Protected sealing clears the
    // persistent input immediately through `clear`, while this copy permits exactly one
    // re-seal if the server reports that its record mapping changed.
    let capturedValue = value;
    const clear = () =>
      adding ? setNewValue('') : setDrafts((values) => {
        const next = new Map(values);
        next.set(field, '');
        return next;
      });
    const sealAndWrite = async (secret: VaultSecret, targetField: string, wasDefault: boolean) => {
      const latestDefault = vaultDefaultField(secret);
      if (!wasDefault && latestDefault === targetField) throw conflict();
      const exists = vaultFieldNames(secret).includes(targetField);
      if (adding ? exists : !exists) throw conflict();
      const record = vaultRecordName(secret.name, targetField, latestDefault);
      let cryptoFields: Omit<VaultFieldValuePayload, 'record' | 'expected_updated_at'>;
      if (isProtected) {
        const valueRef = { current: capturedValue };
        const sealed = await vault.sealValue(record, 'static', { valueRef, clear });
        cryptoFields = { sealed: sealed.envelope };
      } else {
        cryptoFields = {
          blind_box: await sealBlindBox(capturedValue, await api.getVaultPubkey(), standardCreateBlindBoxContext(record)),
        };
      }
      try {
        return await api.updateVaultSecretField(secret.name, targetField, {
          record,
          ...cryptoFields,
          expected_updated_at: secret.updated_at,
        }, { handleError: false });
      } catch (error) {
        if (hasErrorCode(error, 'record_mismatch')) return { ok: false, code: 'record_mismatch' };
        if (hasErrorCode(error, 'secret_conflict')) return { ok: false, code: 'secret_conflict' };
        throw error;
      }
    };
    void mutate(async () => {
      try {
        const wasDefault = field === defaultField;
        let result = await sealAndWrite(current, field, wasDefault);
        if (hasErrorCode(result, 'record_mismatch')) {
          const next = await freshSecret();
          result = await sealAndWrite(next, wasDefault ? vaultDefaultField(next) : field, wasDefault);
          if (hasErrorCode(result, 'record_mismatch')) throw conflict();
        }
        if (result.ok && result.secret) {
          clear();
          if (adding) setNewField(null);
        }
        return result;
      } finally {
        capturedValue = '';
      }
    });
  };

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
                  onChange={(event) => {
                    defaultLabelDirtyRef.current = true;
                    setDefaultLabel(event.target.value);
                  }}
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
            <Input type="password" value={drafts.get(field) ?? ''} autoComplete="off" disabled={locked}
              aria-label={t('vaults.fields.value', { field })}
              placeholder={t('vaults.fields.replacePlaceholder')}
              onChange={(event) => setDrafts((values) => {
                const next = new Map(values);
                next.set(field, event.target.value);
                return next;
              })} />
            <Button type="button" variant="secondary" size="sm"
              aria-label={t('vaults.fields.saveNamed', { field })}
              disabled={locked || !canWriteValue || !drafts.get(field)}
              onClick={() => saveValue(field, drafts.get(field) ?? '')}>{t('vaults.fields.save')}</Button>
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
          await mutate(() => api.deleteVaultSecretField(current.name, removeField, { expected_updated_at: current.updated_at }));
          setRemoveField(null);
        }} />
    </div>
  );
}
