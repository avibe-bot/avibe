import { mergeTags } from './vaultTags';

export type FetchAuthMode = 'bearer' | 'header' | 'query';

/**
 * The value-free body sent to `PATCH /api/vault/secrets/<name>` when editing a
 * secret's metadata. The endpoint accepts ONLY `description`, `tags` and `policy`
 * (see vibe/api.py `_VAULT_METADATA_ALLOWED_FIELDS`) — skill associations travel as
 * reserved `skill:<name>` entries INSIDE `tags`, not a separate `links` field (that
 * is create-only and is rejected here with a 409). `description: null` clears it;
 * `tags: []` clears all tags; `policy` replaces the visible fetch policy
 * (allowed_hosts + auth) while the backend preserves internal keys such as
 * `always_ask` (storage/vault_service.update_secret_metadata).
 */
export type VaultMetadataPatch = {
  description: string | null;
  tags: string[];
  policy: Record<string, unknown>;
};

/**
 * Assemble the metadata PATCH body from the edit form's field state. Mirrors the
 * create form's policy assembly for the user-visible fetch fields, but omits value,
 * kind, protection and `always_ask` (all owned elsewhere). Auth-name validity is the
 * caller's responsibility (shared submit-time validation); this only shapes the body.
 */
export function buildMetadataPatch(input: {
  description: string;
  tags: string[];
  allowHosts: string[];
  fetchAuthMode: FetchAuthMode;
  fetchAuthName: string;
  preserveBearerAuth?: boolean;
}): VaultMetadataPatch {
  const policy: Record<string, unknown> = {};
  if (input.allowHosts.length) policy.allowed_hosts = input.allowHosts;
  const authName = input.fetchAuthName.trim();
  if (input.fetchAuthMode === 'bearer' && input.preserveBearerAuth) policy.auth = { type: 'bearer' };
  else if (input.fetchAuthMode === 'header') policy.auth = { type: 'header', name: authName };
  else if (input.fetchAuthMode === 'query') policy.auth = { type: 'query', name: authName };
  return {
    description: input.description.trim() || null,
    // Skill tags stay inline in `tags` (backend derives skill scopes from the `skill:`
    // prefix); no separate `links` field on the PATCH path — it would be rejected (409).
    tags: mergeTags(input.tags, []),
    policy,
  };
}
