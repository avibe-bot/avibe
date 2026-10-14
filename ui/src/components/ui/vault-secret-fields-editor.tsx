import { useCallback, useEffect, useRef, useState } from 'react';
import { Loader2, Plus, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useApi, type VaultFieldValuePayload, type VaultSecret } from '@/context/ApiContext';
import { sealBlindBox, standardCreateBlindBoxContext } from '@/lib/vaultCrypto';
import { vaultErrorMessage } from '@/lib/vaultErrors';
import { useProtectedVault } from '@/lib/useProtectedVault';
import { openVaultsInBrowser } from '@/lib/vaultBrowserHandoff';
import { MAX_VAULT_FIELDS, VAULT_FIELD_PATTERN, vaultDefaultField, vaultFieldNames, vaultRecordName } from '@/lib/vaultFields';
import { Badge } from './badge';
import { Button } from './button';
import { ConfirmDialog } from './confirm-dialog';
import { Input } from './input';
import { VaultProtectedUnlock } from './vault-protected-unlock';

type DraftReconciliation = {
  drafts: Map<string, string>;
  removed: string[];
};

function reconcileDrafts(previous: VaultSecret, next: VaultSecret, drafts: ReadonlyMap<string, string>): DraftReconciliation {
  const reconciled = new Map(drafts);
  const previousDefault = vaultDefaultField(previous);
  const nextDefault = vaultDefaultField(next);
  if (previousDefault !== nextDefault && reconciled.has(previousDefault)) {
    reconciled.set(nextDefault, reconciled.get(previousDefault) ?? '');
    reconciled.delete(previousDefault);
  }

  const names = new Set(vaultFieldNames(next));
  const removed: string[] = [];
  for (const name of reconciled.keys()) {
    if (!names.has(name)) {
      const value = reconciled.get(name);
      reconciled.delete(name);
      if (value) removed.push(name);
    }
  }
  return { drafts: reconciled, removed };
}

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
  const truthRevisionRef = useRef(0);
  const initialSecretRef = useRef(secret);
  const [drafts, setDraftsState] = useState<Map<string, string>>(() => new Map());
  const draftsRef = useRef<Map<string, string>>(new Map());
  const [defaultLabel, setDefaultLabel] = useState(vaultDefaultField(secret));
  const defaultLabelDirtyRef = useRef(false);
  const [newField, setNewField] = useState<string | null>(null);
  const [newValue, setNewValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [removeField, setRemoveField] = useState<string | null>(null);
  const [draftNotice, setDraftNotice] = useState<string | null>(null);
  const fields = vaultFieldNames(current);
  const defaultField = vaultDefaultField(current);
  const isProtected = current.protection === 'protected';
  const locked = disabled || busy;
  const fieldsCorrupt = current.fields_state === 'corrupt';
  const fieldMutationLocked = locked || fieldsCorrupt;
  const canWriteValue = !isProtected || vault.status === 'unlocked';

  useEffect(() => {
    if (isProtected) void refresh();
  }, [isProtected, refresh]);

  const hasErrorCode = (result: unknown, code: string): boolean =>
    typeof result === 'object' && result !== null && 'code' in result && result.code === code;
  const conflict = () => new Error(t('vaults.fields.recordConflict'));
  const updateDrafts = useCallback((next: Map<string, string>) => {
    draftsRef.current = next;
    setDraftsState(next);
  }, []);
  const applyTruth = useCallback((next: VaultSecret, ownRename = false) => {
    const previous = currentRef.current;
    currentRef.current = next;
    truthRevisionRef.current += 1;
    setCurrent(next);
    const nextDefault = vaultDefaultField(next);
    if (ownRename || !defaultLabelDirtyRef.current) {
      setDefaultLabel(nextDefault);
    }
    if (ownRename) defaultLabelDirtyRef.current = false;
    const reconciled = reconcileDrafts(previous, next, draftsRef.current);
    updateDrafts(reconciled.drafts);
    if (reconciled.removed.length > 0) {
      setDraftNotice(t('vaults.fields.draftsDropped', { fields: reconciled.removed.join(', ') }));
    } else {
      setDraftNotice(null);
    }
  }, [t, updateDrafts]);

  const refetchSequenceRef = useRef(0);
  const invalidatePendingRefetches = useCallback(() => {
    refetchSequenceRef.current += 1;
  }, []);
  type RefetchResult = {
    status: 'applied' | 'superseded' | 'missing';
    entry: VaultSecret | null;
  };
  const refetch = useCallback(async (): Promise<RefetchResult> => {
    const sequence = ++refetchSequenceRef.current;
    try {
      const result = await api.listVaultSecrets({ cache: false });
      const next = result.secrets.find((entry) => entry.name === currentRef.current.name) ?? null;
      if (sequence !== refetchSequenceRef.current) return { status: 'superseded', entry: next };
      if (!next) return { status: 'missing', entry: null };
      applyTruth(next);
      return { status: 'applied', entry: next };
    } catch (refetchError) {
      if (sequence !== refetchSequenceRef.current) return { status: 'superseded', entry: null };
      throw refetchError;
    }
  }, [api, applyTruth]);

  const handleRefetchResult = useCallback((result: RefetchResult) => {
    if (result.status === 'missing') setError(t('errors.secret_not_found'));
  }, [t]);

  const surfaceRefetchError = useCallback((refetchError: unknown) => {
    setError(vaultErrorMessage(t, refetchError, t('vaults.dialog.errors.updateFailed')));
  }, [t]);

  useEffect(() => {
    const refreshFromEvent = () => {
      void refetch().then(handleRefetchResult).catch(surfaceRefetchError);
    };
    const disconnect = api.connectWorkbenchEvents({
      onConnected: refreshFromEvent,
      onVaultsUpdated: (data) => {
        if (!data.secret_name || data.secret_name === currentRef.current.name) refreshFromEvent();
      },
    });
    return () => disconnect?.();
  }, [api, refetch, handleRefetchResult, surfaceRefetchError]);

  useEffect(() => {
    if (secret === initialSecretRef.current) return;
    initialSecretRef.current = secret;
    // Parent updates are only a signal to read server truth; the mounted editor
    // never adopts the prop directly.
    if (secret.name !== currentRef.current.name) return;
    void refetch().then(handleRefetchResult).catch(surfaceRefetchError);
  }, [secret, refetch, handleRefetchResult, surfaceRefetchError]);

  useEffect(() => () => {
    refetchSequenceRef.current += 1;
  }, []);

  const mutate = async (
    operation: () => Promise<{ ok: boolean; secret?: VaultSecret; message?: string; code?: string }>,
    renamed = false,
    operationRefetched: () => boolean = () => false,
  ) => {
    if (locked) return;
    const writeStartTruthRevision = truthRevisionRef.current;
    const writeStartSequence = refetchSequenceRef.current;
    setBusy(true);
    onBusyChange(true);
    setError(null);
    setSaved(false);
    setDraftNotice(null);
    try {
      const result = await operation();
      if (!result.ok || !result.secret) {
        throw Object.assign(new Error(vaultErrorMessage(t, result, t('vaults.dialog.errors.updateFailed'))), { code: result.code });
      }
      const next = result.secret;
      const needsCatchup = (
        refetchSequenceRef.current !== writeStartSequence
        || truthRevisionRef.current !== writeStartTruthRevision
      ) && !operationRefetched();
      invalidatePendingRefetches();
      applyTruth(next, renamed);
      setSaved(true);
      if (needsCatchup) {
        try {
          const refreshed = await refetch();
          if (refreshed.status === 'missing') {
            setError(t('errors.secret_not_found'));
          }
        } catch (catchupError) {
          surfaceRefetchError(catchupError);
        }
      }
    } catch (err) {
      if (hasErrorCode(err, 'secret_conflict')) {
        try {
          const refreshed = await refetch();
          if (refreshed.status === 'missing') {
            setError(t('errors.secret_not_found'));
          } else {
            setError(t('vaults.fields.secretConflict'));
          }
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
    const clear = (targetField = field) => {
      if (adding) {
        setNewValue('');
        return;
      }
      const next = new Map(draftsRef.current);
      next.set(targetField, '');
      updateDrafts(next);
    };
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
    let refetchedForRetry = false;
    void mutate(async () => {
      try {
        const wasDefault = field === defaultField;
        let savedField = field;
        let result = await sealAndWrite(current, field, wasDefault);
        if (hasErrorCode(result, 'record_mismatch')) {
          refetchedForRetry = true;
          const refreshed = await refetch();
          if (!refreshed.entry) {
            setError(t('errors.secret_not_found'));
            return { ok: false, code: 'secret_not_found' };
          }
          savedField = wasDefault ? vaultDefaultField(refreshed.entry) : field;
          result = await sealAndWrite(refreshed.entry, savedField, wasDefault);
          if (hasErrorCode(result, 'record_mismatch')) throw conflict();
        }
        if (result.ok && result.secret) {
          clear(savedField);
          if (adding) setNewField(null);
        }
        return result;
      } finally {
        capturedValue = '';
      }
    }, false, () => refetchedForRetry);
  };

  return (
    <div className="flex flex-col gap-3" onKeyDown={(event) => {
      // These inputs save through their field buttons, not the enclosing metadata form.
      if (event.key === 'Enter' && event.target instanceof HTMLInputElement) event.preventDefault();
    }}>
      <span className="text-[13px] font-medium">{t('vaults.fields.title')}</span>
      <p className="text-[11px] text-muted-foreground">{t('vaults.fields.editHelp')}</p>
      {draftNotice && (
        <p role="status" className="text-xs text-muted-foreground">{draftNotice}</p>
      )}
      {fieldsCorrupt && (
        <p role="alert" className="text-xs text-destructive-ink">{t('errors.fields_corrupt')}</p>
      )}
      {fields.map((field) => (
        <div key={field} className="flex flex-col gap-2 rounded-lg border border-border p-3">
          <div className="flex items-center gap-2">
            {field === defaultField ? (
              <>
                <Input value={defaultLabel} disabled={fieldMutationLocked}
                  aria-label={t('vaults.fields.defaultName')}
                  onChange={(event) => {
                    defaultLabelDirtyRef.current = true;
                    setDefaultLabel(event.target.value);
                  }}
                  className="min-w-0 font-mono" />
                <Badge variant="secondary">{t('vaults.fields.default')}</Badge>
                <Button type="button" size="sm" variant="ghost"
                  disabled={fieldMutationLocked || defaultLabel === defaultField || !VAULT_FIELD_PATTERN.test(defaultLabel) || fields.includes(defaultLabel)}
                  onClick={() => void mutate(
                    () => api.updateVaultSecret(current.name, { default_field: defaultLabel }, { handleError: false }),
                    true,
                  )}>
                  {t('vaults.fields.rename')}
                </Button>
              </>
            ) : (
              <>
                <span className="min-w-0 flex-1 break-all font-mono text-xs">{field}</span>
                <Button type="button" size="icon" variant="ghost" disabled={fieldMutationLocked}
                  aria-label={t('vaults.fields.remove', { field })} onClick={() => setRemoveField(field)}>
                  <X className="size-4" />
                </Button>
              </>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Input type="password" value={drafts.get(field) ?? ''} autoComplete="off" disabled={fieldMutationLocked}
              aria-label={t('vaults.fields.value', { field })}
              placeholder={t('vaults.fields.replacePlaceholder')}
              onChange={(event) => {
                const next = new Map(draftsRef.current);
                next.set(field, event.target.value);
                updateDrafts(next);
              }} />
            <Button type="button" variant="secondary" size="sm"
              aria-label={t('vaults.fields.saveNamed', { field })}
              disabled={fieldMutationLocked || !canWriteValue || !drafts.get(field)}
              onClick={() => saveValue(field, drafts.get(field) ?? '')}>{t('vaults.fields.save')}</Button>
          </div>
        </div>
      ))}
      {newField !== null ? (
        <div className="flex flex-col gap-2 rounded-lg border border-border p-3">
          <Input value={newField} disabled={fieldMutationLocked} aria-label={t('vaults.fields.newName')}
            placeholder={t('vaults.fields.namePlaceholder')} onChange={(event) => setNewField(event.target.value)} />
          <Input type="password" value={newValue} autoComplete="off" disabled={fieldMutationLocked}
            aria-label={t('vaults.fields.newValue')} placeholder={t('vaults.dialog.valuePlaceholder')}
            onChange={(event) => setNewValue(event.target.value)} />
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" size="sm" disabled={locked}
              onClick={() => { setNewField(null); setNewValue(''); }}>{t('vaults.dialog.cancel')}</Button>
            <Button type="button" size="sm"
              disabled={fieldMutationLocked || !canWriteValue || !newValue || !VAULT_FIELD_PATTERN.test(newField) || fields.includes(newField)}
              onClick={() => saveValue(newField, newValue, true)}>{t('vaults.fields.save')}</Button>
          </div>
          <span className="text-[11px] text-muted-foreground">{t('vaults.fields.namesHelp')}</span>
        </div>
      ) : (
        <Button type="button" variant="ghost" size="sm" className="self-start" disabled={fieldMutationLocked || fields.length >= MAX_VAULT_FIELDS}
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
          if (!removeField || fieldsCorrupt) return;
          await mutate(() => api.deleteVaultSecretField(current.name, removeField, { expected_updated_at: current.updated_at }));
          setRemoveField(null);
        }} />
    </div>
  );
}
