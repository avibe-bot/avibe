import type { VaultSecret } from '../context/ApiContext';
import { partitionTags } from './vaultTags';

/** Same term semantics as `vibe vault find`: literal, case-insensitive AND. */
export function vaultSearchTerms(query: string): string[] {
  return query.trim().toLowerCase().split(/\s+/).filter(Boolean);
}

/** Only explicitly public metadata belongs here, never an entire secret or policy. */
export function matchesVaultQuery(secret: VaultSecret, terms: readonly string[], labels: readonly string[]): boolean {
  const { tags, skills } = partitionTags(secret.tags);
  const auth = secret.policy.auth as { type?: unknown; name?: unknown } | undefined;
  const text = [
    secret.name, secret.description, secret.kind, secret.protection, secret.signer_kind,
    ...tags, ...skills, ...labels,
    ...(Array.isArray(secret.policy.allowed_hosts) ? secret.policy.allowed_hosts : []),
    auth?.type, auth?.name,
    ...Object.values(secret.signing_addresses ?? {}),
  ].filter((value): value is string => typeof value === 'string').join(' ').toLowerCase();
  return terms.every((term) => text.includes(term));
}
