import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useApi, type VaultFieldsBatchPayload, type VaultSecret } from '@/context/ApiContext';
import { sealBlindBox, standardCreateBlindBoxContext } from '@/lib/vaultCrypto';
import { vaultErrorMessage } from '@/lib/vaultErrors';
import { useProtectedVault } from '@/lib/useProtectedVault';
import { vaultEntryIssue } from '@/lib/vaultEntry';
import { openVaultsInBrowser } from '@/lib/vaultBrowserHandoff';
import { MAX_VAULT_FIELDS, vaultDefaultField, vaultFieldNameIssue, vaultFieldNames, vaultRecordName } from '@/lib/vaultFields';
import { isAvaultAuthorizationRequired } from '@/lib/useAvaultAuthorization';
import { AvaultAuthorizationNotice } from './avault-authorization-notice';
import { Button } from './button';
import {
  VaultFieldList,
  VaultFieldNameRule,
  VaultFieldReferences,
  VaultFieldRemoveConfirm,
  VaultFieldRow,
  VaultFieldsHeader,
} from './vault-field-list';
import { VaultProtectedUnlock } from './vault-protected-unlock';

type DraftReconciliation = {
  drafts: Map<string, string>;
  removed: string[];
};

function reconcileDrafts(previous: VaultSecret, next: VaultSecret, drafts: ReadonlyMap<string, string>): DraftReconciliation {
  // Labels can move between bare-default and dotted-extra records. Preserve drafts only for
  // the same record role, never by reusing a label that now identifies a different record.
  const reconciled = new Map<string, string>();
  const previousDefault = vaultDefaultField(previous);
  const nextDefault = vaultDefaultField(next);
  const nextExtras = new Set(vaultFieldNames(next).filter((field) => field !== nextDefault));
  const removed: string[] = [];
  for (const [name, value] of drafts) {
    if (name === previousDefault) {
      reconciled.set(nextDefault, value);
    } else if (nextExtras.has(name)) {
      reconciled.set(name, value);
    } else if (value) {
      removed.push(name);
    }
  }
  return { drafts: reconciled, removed };
}

/** The same final-layout constraints apply to rendered drafts and every pre-seal attempt.
 * Removed extras free capacity, but not their names: backend changes/removals are disjoint. */
function fieldEditLayout(truth: VaultSecret, finalDefault: string, newField: string | null, removed: readonly string[]) {
  const defaultField = vaultDefaultField(truth);
  const names = vaultFieldNames(truth);
  const extras = names.filter((field) => field !== defaultField);
  const labelIssue = vaultFieldNameIssue(finalDefault, newField === null ? extras : [...extras, newField]);
  const newIssue = newField === null ? null : vaultFieldNameIssue(newField, [...extras, finalDefault]);
  const keptCount = names.filter((field) => !removed.includes(field)).length;
  const count = keptCount + (newField === null ? 0 : 1);
  const overLimit = count > MAX_VAULT_FIELDS;
  const invalidRemoval = removed.some((field) => !extras.includes(field) || field === finalDefault);
  return { labelIssue, newIssue, keptCount, count, overLimit,
    valid: labelIssue === null && newIssue === null && !invalidRemoval && !overLimit };
}

/** What the edit dialog's Save calls: commit every field change at once, or nothing. */
export type VaultFieldsCommit = {
  /** Whether anything is waiting to be saved. */
  dirty: () => boolean;
  /** Writes all changes in one atomic request; resolves true when there was nothing to do or
   *  the server accepted them, false when they were not written (the error is shown here). */
  commit: () => Promise<boolean>;
};

/**
 * The fields of an existing static secret (ui/design: vault/generate-secret). Every change —
 * a new value, the default field's new name, an added field, a removed field — is a draft until
 * the dialog's Save, which commits them all in one atomic request (POST …/fields); Cancel drops
 * them. There is no Save per row.
 */
export function VaultSecretFieldsEditor({
  secret, disabled, onBusyChange, commitRef, onChange,
}: {
  secret: VaultSecret;
  disabled: boolean;
  onBusyChange: (busy: boolean) => void;
  /** Filled with this editor's commit, for the dialog's Save. */
  commitRef: { current: VaultFieldsCommit | null };
  /** Reports whether there are changes and whether they can be saved as they stand. */
  onChange?: (state: { dirty: boolean; valid: boolean }) => void;
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
  // Saved extra fields the user removed in this dialog; they go with the Save.
  const [removed, setRemoved] = useState<string[]>([]);
  const [focusedName, setFocusedName] = useState<'default' | 'new' | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The last write failed because avault needs its one-time Keychain authorization.
  const [needsAvaultAuthorization, setNeedsAvaultAuthorization] = useState(false);
  const [saved, setSaved] = useState(false);
  const [draftNotice, setDraftNotice] = useState<string | null>(null);
  const fields = vaultFieldNames(current);
  const defaultField = vaultDefaultField(current);
  const isProtected = current.protection === 'protected';
  const entryIssue = vaultEntryIssue();
  const locked = disabled || busy;
  const fieldsCorrupt = current.fields_state === 'corrupt';
  const fieldMutationLocked = locked || fieldsCorrupt;
  const canWriteValue = !isProtected || (!entryIssue && vault.status === 'unlocked');

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
    // A staged removal another write already did (or that became the default) is no longer ours to send.
    const nextNames = vaultFieldNames(next);
    setRemoved((staged) => {
      const kept = staged.filter((field) => nextNames.includes(field) && field !== nextDefault);
      return kept.length === staged.length ? staged : kept;
    });
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

  /** Seal one value for `record`: in the sandbox for Protected (it clears the page's copy through
   *  `clear`), as a blind box to avault for Standard. */
  const sealFor = async (record: string, value: string, clear: () => void) => {
    if (isProtected) {
      const sealed = await vault.sealValue(record, 'static', { valueRef: { current: value }, clear });
      return { sealed: sealed.envelope };
    }
    return { blind_box: await sealBlindBox(value, await api.getVaultPubkey(), standardCreateBlindBoxContext(record)) };
  };

  // One commit for the whole dialog: rename the default field, write every new value (under the
  // final labels), add the new field, remove the removed ones — all or nothing. The values are
  // captured once and kept only until the request settles, which allows exactly one re-seal if the
  // server reports that a record's name changed meanwhile.
  const commitAll = async (): Promise<boolean> => {
    if (fieldsCorrupt || locked || !valid) return false;
    const captured = new Map<string, string>();
    for (const [field, value] of draftsRef.current) if (value && !removed.includes(field)) captured.set(field, value);
    const adding = newField !== null && newValue ? { field: newField, value: newValue } : null;
    // The default field as the user saw it: its draft follows the default, any other draft must
    // stay an extra field (if another write made it the default meanwhile, that is a conflict).
    const capturedDefault = vaultDefaultField(currentRef.current);
    const renameTo = defaultLabel !== capturedDefault ? defaultLabel : null;
    const removing = [...removed];
    if (!captured.size && !adding && !renameTo && !removing.length) return true;
    const clearAll = () => {
      updateDrafts(new Map());
      setNewValue('');
    };
    let refetchedForRetry = false;
    const write = async (truth: VaultSecret) => {
      const oldDefault = vaultDefaultField(truth);
      const finalDefault = renameTo ?? oldDefault;
      const names = vaultFieldNames(truth);
      const layout = fieldEditLayout(truth, finalDefault, adding?.field ?? null, removing);
      if (layout.overLimit) throw new Error(t('errors.too_many_fields'));
      if (!layout.valid) throw conflict();
      const values: NonNullable<VaultFieldsBatchPayload['values']> = [];
      const entries: Array<readonly [string, string]> = [];
      for (const [field, value] of captured) {
        if (field === capturedDefault) {
          entries.push([finalDefault, value]);
        } else {
          if (field === oldDefault || !names.includes(field)) throw conflict();
          entries.push([field, value]);
        }
      }
      if (adding) entries.push([adding.field, adding.value]);
      for (const [field, value] of entries) {
        const record = vaultRecordName(truth.name, field, finalDefault);
        values.push({ field, record, ...(await sealFor(record, value, clearAll)) });
      }
      try {
        return await api.updateVaultSecretFields(truth.name, {
          expected_updated_at: truth.updated_at,
          ...(renameTo ? { default_field: renameTo } : {}),
          values,
          remove: removing,
        }, { handleError: false });
      } catch (writeError) {
        if (hasErrorCode(writeError, 'record_mismatch')) return { ok: false, code: 'record_mismatch' };
        throw writeError;
      }
    };
    try {
      return await mutate(async () => {
        let result = await write(currentRef.current);
        if (hasErrorCode(result, 'record_mismatch')) {
          refetchedForRetry = true;
          const refreshed = await refetch();
          if (!refreshed.entry) {
            setError(t('errors.secret_not_found'));
            return { ok: false, code: 'secret_not_found' };
          }
          result = await write(refreshed.entry);
          if (hasErrorCode(result, 'record_mismatch')) throw conflict();
        }
        if (result.ok && result.secret) {
          clearAll();
          setNewField(null);
          setNewNameTouched(false);
          setRemoved([]);
        }
        return result;
      }, Boolean(renameTo), () => refetchedForRetry);
    } finally {
      captured.clear();
    }
  };

  const removeSavedField = (field: string) => {
    if (fieldsCorrupt) return;
    // The field's draft is kept, so Undo brings it back as it was; Save skips it while staged.
    setRemoved((current) => (current.includes(field) ? current : [...current, field]));
  };
  const discardNewField = () => {
    setNewField(null);
    setNewValue('');
    setNewNameTouched(false);
  };

  // Names are checked against the names the secret will have after Save: the default's new label,
  // the other fields, and the field being added.
  const layout = fieldEditLayout(current, defaultLabel, newField, removed);
  const { labelIssue, newIssue, keptCount } = layout;
  const focusedIssue = focusedName === 'default' ? labelIssue : focusedName === 'new' ? newIssue : null;
  const showNameRule = focusedName !== null && focusedIssue !== 'nameInvalid' && focusedIssue !== 'nameTaken';
  const renaming = defaultLabel !== defaultField;
  const hasValues = [...drafts].some(([field, value]) => Boolean(value) && !removed.includes(field));
  const dirty = hasValues || renaming || removed.length > 0 || (newField !== null && (Boolean(newValue) || Boolean(newField)));
  // Corrupt field metadata blocks field changes, not the dialog: with nothing drafted here its
  // Save still saves description, tags and policy.
  const valid = !dirty || (!fieldsCorrupt
    && layout.valid
    && (newField === null || Boolean(newValue))
    && (!(hasValues || (newField !== null && newValue)) || canWriteValue));
  // The latest commit for the dialog's Save, refreshed after every render.
  useLayoutEffect(() => {
    commitRef.current = { dirty: () => dirty, commit: commitAll };
  });
  useEffect(() => {
    onChange?.({ dirty, valid });
  }, [onChange, dirty, valid]);

  return (
    <div className="flex flex-col gap-2" onKeyDown={(event) => {
      // Enter in a field input doesn't submit the dialog; its Save button does.
      if (event.key === 'Enter' && event.target instanceof HTMLInputElement) event.preventDefault();
    }}>
      <VaultFieldsHeader count={layout.count} note={t('vaults.fields.editHelp')} />
      {layout.overLimit && <p role="alert" className="text-xs text-destructive-ink">{t('errors.too_many_fields')}</p>}
      {draftNotice && (
        <p role="status" className="text-xs text-muted">{draftNotice}</p>
      )}
      {fieldsCorrupt && (
        <p role="alert" className="text-xs text-destructive-ink">{t('errors.fields_corrupt')}</p>
      )}
      <VaultFieldList
        onAdd={() => setNewField('')}
        addDisabled={fieldMutationLocked || newField !== null || keptCount >= MAX_VAULT_FIELDS}
      >
        {fields.filter((field) => !removed.includes(field)).map((field) => {
          const isDefault = field === defaultField;
          const draft = drafts.get(field) ?? '';
          const renamingRow = isDefault && renaming;
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
                  const nextDrafts = new Map(draftsRef.current);
                  nextDrafts.set(field, next);
                  updateDrafts(nextDrafts);
                },
              }}
              remove={isDefault ? undefined : {
                label: t('vaults.fields.remove', { field }),
                confirm: <VaultFieldRemoveConfirm field={field} revokesGrants />,
                // Staged: the field goes with the dialog's Save, not now.
                onRemove: () => removeSavedField(field),
              }}
              issue={renamingRow ? labelIssue : null}
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
              onChange: setNewField,
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
              onChange: setNewValue,
            }}
            remove={{ label: t('vaults.fields.discardNew'), onRemove: discardNewField }}
            issue={newIssue === 'nameRequired' && !newNameTouched && !newValue ? null : newIssue}
            disabled={fieldMutationLocked}
          />
        )}
      </VaultFieldList>
      {removed.length > 0 ? (
        <p role="status" className="flex flex-wrap items-center gap-x-2 text-xs text-muted">
          {t('vaults.fields.removeOnSave', { fields: removed.join(', ') })}
          <Button type="button" variant="ghost" size="xs" className="h-6 px-1.5" disabled={fieldMutationLocked} onClick={() => setRemoved([])}>
            {t('vaults.fields.undoRemove')}
          </Button>
        </p>
      ) : null}
      <VaultFieldNameRule shown={showNameRule} />
      <VaultFieldReferences secretName={current.name} fields={fields} defaultField={defaultField} />
      {isProtected && <VaultProtectedUnlock warnDraftDiscard vault={vault} secretName={current.name}
        onContinueInBrowser={() => openVaultsInBrowser({ kind: 'edit', secretName: current.name })} />}
      {busy && <Loader2 className="size-4 animate-spin text-muted" />}
      {saved && <p role="status" className="text-xs text-mint-ink">{t('vaults.fields.saved')}</p>}
      {needsAvaultAuthorization && <AvaultAuthorizationNotice forceVisible />}
      {error && !(layout.overLimit && error === t('errors.too_many_fields')) && <p role="alert" className="text-xs text-destructive-ink">{error}</p>}
    </div>
  );
}
