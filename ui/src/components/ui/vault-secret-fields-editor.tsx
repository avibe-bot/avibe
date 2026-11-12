import { useCallback, useEffect, useRef, useState } from 'react';
import { Loader2, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useApi, type VaultFieldValuePayload, type VaultSecret } from '@/context/ApiContext';
import { sealBlindBox, standardCreateBlindBoxContext } from '@/lib/vaultCrypto';
import { vaultErrorMessage } from '@/lib/vaultErrors';
import { useProtectedVault } from '@/lib/useProtectedVault';
import type { VaultSandboxSealResult } from '@/lib/vaultSandboxClient';
import { DEFAULT_STATIC_SECRET_GENERATION_RECIPE, generateStaticSecret, normalizeStaticSecretGenerationRecipe, type StaticSecretGenerationRecipe } from '@/lib/vaultSecretGeneration';
import { vaultEntryIssue } from '@/lib/vaultEntry';
import { openVaultsInBrowser } from '@/lib/vaultBrowserHandoff';
import { MAX_VAULT_FIELDS, vaultDefaultField, vaultFieldNameIssue, vaultFieldNames, vaultRecordName } from '@/lib/vaultFields';
import { isAvaultAuthorizationRequired } from '@/lib/useAvaultAuthorization';
import { AvaultAuthorizationNotice } from './avault-authorization-notice';
import { Button } from './button';
import {
  VaultFieldList,
  VaultFieldReferences,
  VaultFieldRemoveConfirm,
  VaultFieldRow,
  VaultFieldsHeader,
} from './vault-field-list';
import { VaultProtectedUnlock } from './vault-protected-unlock';
import { VaultSecretGenerationOptions } from './vault-secret-generation-options';

type DraftReconciliation = {
  drafts: Map<string, string>;
  removed: string[];
};

type ProtectedGeneratedDraft = {
  record: string;
  recipe: StaticSecretGenerationRecipe;
  sealed: VaultSandboxSealResult;
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
  const [newNameTouched, setNewNameTouched] = useState(false);
  const [focusedName, setFocusedName] = useState<'default' | 'new' | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The last write failed because avault needs its one-time Keychain authorization.
  const [needsAvaultAuthorization, setNeedsAvaultAuthorization] = useState(false);
  const [saved, setSaved] = useState(false);
  const [draftNotice, setDraftNotice] = useState<string | null>(null);
  const [generationRecipe, setGenerationRecipe] = useState<StaticSecretGenerationRecipe>({ ...DEFAULT_STATIC_SECRET_GENERATION_RECIPE });
  const [protectedGeneratedDrafts, setProtectedGeneratedDrafts] = useState<Map<string, ProtectedGeneratedDraft>>(() => new Map());
  const [generatingField, setGeneratingField] = useState<string | null>(null);
  const generatedStandardFieldsRef = useRef(new Set<string>());
  const fields = vaultFieldNames(current);
  const defaultField = vaultDefaultField(current);
  const isProtected = current.protection === 'protected';
  const entryIssue = vaultEntryIssue();
  const locked = disabled || busy;
  const fieldsCorrupt = current.fields_state === 'corrupt';
  const fieldMutationLocked = locked || fieldsCorrupt || generatingField !== null;
  const canWriteValue = !isProtected || (!entryIssue && vault.status === 'unlocked');

  const generatedDraftKey = (field: string, adding = false) => `${adding ? 'new' : 'field'}:${field}`;
  const newGenerationKey = generatedDraftKey(newField ?? '', true);
  const clearProtectedGeneratedDraft = (key: string) => {
    setProtectedGeneratedDrafts((current) => {
      if (!current.has(key)) return current;
      const next = new Map(current);
      next.delete(key);
      return next;
    });
  };
  const clearGeneratedDraft = (key: string) => {
    generatedStandardFieldsRef.current.delete(key);
    clearProtectedGeneratedDraft(key);
  };
  const changeGenerationRecipe = (nextRecipe: StaticSecretGenerationRecipe) => {
    if (JSON.stringify(nextRecipe) !== JSON.stringify(generationRecipe)) {
      const generated = [...generatedStandardFieldsRef.current];
      if (generated.length > 0) {
        const nextDrafts = new Map(draftsRef.current);
        for (const key of generated) {
          if (key.startsWith('field:')) nextDrafts.set(key.slice('field:'.length), '');
          if (key.startsWith('new:')) setNewValue('');
        }
        updateDrafts(nextDrafts);
        generatedStandardFieldsRef.current.clear();
      }
      setProtectedGeneratedDrafts(new Map());
    }
    setGenerationRecipe(nextRecipe);
  };

  useEffect(() => {
    if (isProtected && !entryIssue) void refresh();
  }, [isProtected, entryIssue, refresh]);

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

  /** Resolves true once the server accepted the write and its result became the editor's truth. */
  const mutate = async (
    operation: () => Promise<{ ok: boolean; secret?: VaultSecret; message?: string; code?: string }>,
    renamed = false,
    operationRefetched: () => boolean = () => false,
  ): Promise<boolean> => {
    if (locked) return false;
    const writeStartTruthRevision = truthRevisionRef.current;
    const writeStartSequence = refetchSequenceRef.current;
    let accepted = false;
    setBusy(true);
    onBusyChange(true);
    setError(null);
    setNeedsAvaultAuthorization(false);
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
      accepted = true;
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
      } else if (isAvaultAuthorizationRequired(err)) {
        setNeedsAvaultAuthorization(true);
      } else {
        setError(vaultErrorMessage(t, err, t('vaults.dialog.errors.updateFailed')));
      }
    } finally {
      setBusy(false);
      onBusyChange(false);
    }
    return accepted;
  };

  const saveValue = (
    field: string,
    value: string,
    adding = false,
    generatedDraft?: ProtectedGeneratedDraft,
    generatedKey = generatedDraftKey(field, adding),
  ) => {
    // Keep this one local copy only until the write settles. Protected sealing clears the
    // persistent input immediately through `clear`, while this copy permits exactly one
    // re-seal if the server reports that its record mapping changed.
    let capturedValue = value;
    const clear = (targetField = field) => {
      if (adding) {
        setNewValue('');
        setNewNameTouched(false);
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
      if (isProtected && generatedDraft) {
        const sealed = generatedDraft.record === record
          ? generatedDraft.sealed
          : await vault.sealValue(record, 'static', { generate: generatedDraft.recipe });
        cryptoFields = { sealed: sealed.envelope };
      } else if (isProtected) {
        const valueRef = { current: capturedValue };
        const sealed = await vault.sealValue(record, 'static', { valueRef, clear });
        cryptoFields = { sealed: sealed.envelope };
      } else {
        cryptoFields = { blind_box: await sealBlindBox(capturedValue, await api.getVaultPubkey(), standardCreateBlindBoxContext(record)) };
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
        // Read the latest truth rather than this render's: a value saved right after its
        // field's rename must be sealed under the renamed record.
        const latest = currentRef.current;
        const wasDefault = field === vaultDefaultField(latest);
        let savedField = field;
        let result = await sealAndWrite(latest, field, wasDefault);
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
          clearGeneratedDraft(generatedKey);
          if (adding) setNewField(null);
        }
        return result;
      } finally {
        capturedValue = '';
      }
    }, false, () => refetchedForRetry);
  };

  const generateField = async (field: string, adding = false) => {
    if (fieldMutationLocked || !canWriteValue) return;
    try {
      const recipe = normalizeStaticSecretGenerationRecipe(generationRecipe);
      const key = generatedDraftKey(field, adding);
      setError(null);
      if (!isProtected) {
        const value = generateStaticSecret(recipe);
        clearGeneratedDraft(key);
        if (adding) {
          setNewValue(value);
        } else {
          const nextDrafts = new Map(draftsRef.current);
          nextDrafts.set(field, value);
          updateDrafts(nextDrafts);
        }
        generatedStandardFieldsRef.current.add(key);
        return;
      }

      const currentSecret = currentRef.current;
      const record = vaultRecordName(currentSecret.name, field, vaultDefaultField(currentSecret));
      setGeneratingField(key);
      try {
        const sealed = await vault.sealValue(record, 'static', { generate: recipe });
        const latestSecret = currentRef.current;
        if (record !== vaultRecordName(latestSecret.name, field, vaultDefaultField(latestSecret))) {
          setError(t('vaults.fields.recordConflict'));
          return;
        }
        setProtectedGeneratedDrafts((current) => new Map(current).set(key, { record, recipe, sealed }));
      } finally {
        setGeneratingField(null);
      }
    } catch (generationError) {
      setError(vaultErrorMessage(t, generationError, t('vaults.dialog.errors.generationFailed')));
    }
  };

  // One Save per row: the default row renames (metadata only) and then writes a typed value
  // under the renamed record; any other row writes its value.
  const saveRow = async (field: string) => {
    const value = draftsRef.current.get(field) ?? '';
    const key = generatedDraftKey(field);
    const generated = protectedGeneratedDrafts.get(key);
    if (field === defaultField && defaultLabel !== defaultField) {
      const renamed = await mutate(
        () => api.updateVaultSecret(current.name, { default_field: defaultLabel }, { handleError: false }),
        true,
      );
      // The rename carried the typed value over to the new label (reconcileDrafts).
      if (renamed && (value || generated)) {
        saveValue(vaultDefaultField(currentRef.current), value, false, generated, key);
      }
      return;
    }
    if (value || generated) saveValue(field, value, false, generated, key);
  };
  const removeSavedField = async (field: string) => {
    if (fieldsCorrupt) return;
    await mutate(() => api.deleteVaultSecretField(current.name, field, { expected_updated_at: current.updated_at }));
  };
  const discardNewField = () => {
    clearGeneratedDraft(generatedDraftKey(newField ?? '', true));
    setNewField(null);
    setNewValue('');
    setNewNameTouched(false);
  };

  const labelIssue = vaultFieldNameIssue(defaultLabel, fields.filter((field) => field !== defaultField));
  const newIssue = newField === null ? null : vaultFieldNameIssue(newField, fields);
  const focusedIssue = focusedName === 'default' ? labelIssue : focusedName === 'new' ? newIssue : null;
  const showNameRule = focusedName !== null && focusedIssue !== 'nameInvalid' && focusedIssue !== 'nameTaken';
  const newProtectedGenerated = newField === null ? undefined : protectedGeneratedDrafts.get(newGenerationKey);

  return (
    <div className="flex flex-col gap-2" onKeyDown={(event) => {
      // These inputs save through their field buttons, not the enclosing metadata form.
      if (event.key === 'Enter' && event.target instanceof HTMLInputElement) event.preventDefault();
    }}>
      <VaultFieldsHeader count={fields.length + (newField === null ? 0 : 1)} note={t('vaults.fields.editHelp')} />
      {draftNotice && (
        <p role="status" className="text-xs text-muted">{draftNotice}</p>
      )}
      {fieldsCorrupt && (
        <p role="alert" className="text-xs text-destructive-ink">{t('errors.fields_corrupt')}</p>
      )}
      <VaultSecretGenerationOptions recipe={generationRecipe} onChange={changeGenerationRecipe} disabled={fieldMutationLocked} />
      <VaultFieldList
        onAdd={() => setNewField('')}
        addDisabled={fieldMutationLocked || newField !== null || fields.length >= MAX_VAULT_FIELDS}
      >
        {fields.map((field) => {
          const isDefault = field === defaultField;
          const draft = drafts.get(field) ?? '';
          const renaming = isDefault && defaultLabel !== defaultField;
          const generationKey = generatedDraftKey(field);
          const generated = protectedGeneratedDrafts.get(generationKey);
          return (
            <VaultFieldRow
              key={field}
              field={field}
              isDefault={isDefault}
              name={isDefault ? {
                editable: true,
                value: defaultLabel,
                label: t('vaults.fields.defaultName'),
                onChange: (next) => {
                  defaultLabelDirtyRef.current = true;
                  clearProtectedGeneratedDraft(generatedDraftKey(field));
                  setDefaultLabel(next);
                },
                onFocus: () => setFocusedName('default'),
                onBlur: () => setFocusedName(null),
              } : { editable: false, title: t('vaults.fields.extraNotRenamable') }}
              value={{
                value: draft,
                label: t('vaults.fields.value', { field }),
                placeholder: t('vaults.fields.replacePlaceholder'),
                onChange: (next) => {
                  clearGeneratedDraft(generationKey);
                  const nextDrafts = new Map(draftsRef.current);
                  nextDrafts.set(field, next);
                  updateDrafts(nextDrafts);
                },
              }}
              action={(
                <div className="flex flex-wrap items-center gap-1.5">
                  {(draft || renaming || generated) && (
                    <Button type="button" size="xs"
                      aria-label={t('vaults.fields.saveNamed', { field })}
                      disabled={fieldMutationLocked || (renaming && labelIssue !== null) || ((Boolean(draft) || Boolean(generated)) && !canWriteValue)}
                      onClick={() => void saveRow(field)}>
                      {t('vaults.fields.save')}
                    </Button>
                  )}
                  <Button type="button" size="xs" variant="secondary"
                    aria-label={t('vaults.fields.generateNamed', { field })}
                    disabled={fieldMutationLocked || !canWriteValue || labelIssue !== null || renaming}
                    onClick={() => void generateField(field)}>
                    {generatingField === generationKey ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
                    {t('vaults.dialog.generateStatic')}
                  </Button>
                  {generated && <span role="status" className="text-[11px] text-mint-ink">{t('vaults.dialog.generatedProtected')}</span>}
                </div>
              )}
              remove={isDefault ? undefined : {
                label: t('vaults.fields.remove', { field }),
                confirm: <VaultFieldRemoveConfirm field={field} revokesGrants />,
                onRemove: () => removeSavedField(field),
              }}
              issue={isDefault && renaming ? labelIssue : null}
              disabled={fieldMutationLocked}
            />
          );
        })}
        {newField !== null && (
          <VaultFieldRow
            field={newField || t('vaults.fields.newName')}
            isDefault={false}
            name={{
              editable: true,
              value: newField,
              label: t('vaults.fields.newName'),
              onChange: (next) => {
                const oldKey = newGenerationKey;
                clearProtectedGeneratedDraft(oldKey);
                if (generatedStandardFieldsRef.current.delete(oldKey)) {
                  generatedStandardFieldsRef.current.add(generatedDraftKey(next, true));
                }
                setNewField(next);
              },
              onFocus: () => setFocusedName('new'),
              onBlur: () => {
                setFocusedName(null);
                setNewNameTouched(true);
              },
              autoFocus: true,
            }}
            value={{
              value: newValue,
              label: t('vaults.fields.newValue'),
              placeholder: t('vaults.dialog.valuePlaceholder'),
              onChange: (next) => {
                clearGeneratedDraft(newGenerationKey);
                setNewValue(next);
              },
            }}
            action={(
              <div className="flex flex-wrap items-center gap-1.5">
                <Button type="button" size="xs" aria-label={t('vaults.fields.saveNew')}
                  disabled={fieldMutationLocked || !canWriteValue || (!newValue && !newProtectedGenerated) || newIssue !== null}
                  onClick={() => saveValue(newField, newValue, true, newProtectedGenerated, newGenerationKey)}>
                  {t('vaults.fields.save')}
                </Button>
                <Button type="button" size="xs" variant="secondary" aria-label={t('vaults.fields.generateNamed', { field: newField || t('vaults.fields.newName') })}
                  disabled={fieldMutationLocked || !canWriteValue || newIssue !== null}
                  onClick={() => void generateField(newField, true)}>
                  {generatingField === newGenerationKey ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
                  {t('vaults.dialog.generateStatic')}
                </Button>
                {newProtectedGenerated && <span role="status" className="text-[11px] text-mint-ink">{t('vaults.dialog.generatedProtected')}</span>}
              </div>
            )}
            remove={{ label: t('vaults.fields.discardNew'), onRemove: discardNewField }}
            issue={newIssue === 'nameRequired' && !newNameTouched && !newValue ? null : newIssue}
            disabled={fieldMutationLocked}
          />
        )}
      </VaultFieldList>
      {showNameRule ? <span className="text-[11px] text-muted">{t('vaults.fields.nameRule')}</span> : null}
      <VaultFieldReferences secretName={current.name} fields={fields} defaultField={defaultField} />
      {isProtected && <VaultProtectedUnlock warnDraftDiscard vault={vault} secretName={current.name}
        onContinueInBrowser={() => openVaultsInBrowser({ kind: 'edit', secretName: current.name })} />}
      {busy && <Loader2 className="size-4 animate-spin text-muted" />}
      {saved && <p role="status" className="text-xs text-mint-ink">{t('vaults.fields.saved')}</p>}
      {needsAvaultAuthorization && <AvaultAuthorizationNotice forceVisible />}
      {error && <p role="alert" className="text-xs text-destructive-ink">{error}</p>}
    </div>
  );
}
