import type { VaultSecret } from '@/context/ApiContext';

export const VAULT_FIELD_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
export const MAX_VAULT_FIELDS = 32;

export type VaultFieldNameIssue = 'nameRequired' | 'nameInvalid' | 'nameTaken';

/** The first naming rule `name` breaks beside its sibling field names, or null. */
export function vaultFieldNameIssue(name: string, siblings: readonly string[]): VaultFieldNameIssue | null {
  if (!name) return 'nameRequired';
  if (!VAULT_FIELD_PATTERN.test(name)) return 'nameInvalid';
  return siblings.includes(name) ? 'nameTaken' : null;
}

export function vaultDefaultField(secret: Pick<VaultSecret, 'default_field'>): string {
  return secret.default_field && VAULT_FIELD_PATTERN.test(secret.default_field) ? secret.default_field : 'value';
}

export function vaultFieldNames(secret: Pick<VaultSecret, 'default_field' | 'fields'>): string[] {
  const defaultField = vaultDefaultField(secret);
  return [defaultField, ...(secret.fields ?? []).filter((field) => field !== defaultField)];
}

export function vaultFieldFingerprint(secret: Pick<VaultSecret, 'default_field' | 'fields' | 'fields_state' | 'updated_at'>): string {
  // Default labels are metadata-only and can change without an envelope version.
  return JSON.stringify([vaultDefaultField(secret), vaultFieldNames(secret), secret.fields_state ?? null, secret.updated_at]);
}

type VaultFieldShape = Pick<VaultSecret, 'default_field' | 'fields' | 'fields_state'> &
  Partial<Pick<VaultSecret, 'kind'>>;

export function vaultHasNamedFieldShape(secret: VaultFieldShape): boolean {
  if (secret.kind === 'keypair') return false;
  const fields = vaultFieldNames(secret);
  return secret.fields_state === 'corrupt'
    || secret.default_field !== 'value'
    || fields.length !== 1
    || fields[0] !== 'value';
}

export function vaultRecordName(name: string, field: string, defaultField: string): string {
  return field === defaultField ? name : `${name}.${field}`;
}
