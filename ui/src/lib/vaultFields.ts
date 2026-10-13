import type { VaultSecret } from '@/context/ApiContext';

export const VAULT_FIELD_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
export const MAX_VAULT_FIELDS = 32;

export function vaultDefaultField(secret: Pick<VaultSecret, 'default_field'>): string {
  return secret.default_field && VAULT_FIELD_PATTERN.test(secret.default_field) ? secret.default_field : 'value';
}

export function vaultFieldNames(secret: Pick<VaultSecret, 'default_field' | 'fields'>): string[] {
  const defaultField = vaultDefaultField(secret);
  return [defaultField, ...(secret.fields ?? []).filter((field) => field !== defaultField)];
}

export function vaultRecordName(name: string, field: string, defaultField: string): string {
  return field === defaultField ? name : `${name}.${field}`;
}
