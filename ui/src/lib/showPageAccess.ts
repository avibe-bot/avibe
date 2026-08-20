import type { PermissionsResponse, ResourceAccessLevel } from '@/features/permissions/types';
export type ShowPageAccess = {
  ok: true;
  mode: 'unmanaged' | 'personal' | 'organization' | 'organization_pending' | 'configuration_unavailable';
  ownership_status: 'unmanaged' | 'created' | 'adopted' | 'unchanged' | 'pending' | 'conflict' | 'configuration_unavailable';
  instance_id: string | null;
  organization_id: string | null;
  policy_organization_id: string | null;
  access_level: ResourceAccessLevel;
  group_ids: string[];
  policy_revision: number | null;
  last_applied_control_plane_revision: number | null;
  can_use: boolean;
  can_manage: boolean;
  can_publish_public: boolean;
};

export type ShowPageAccessProbe =
  | { status: 'granted'; access: ShowPageAccess }
  | { status: 'denied'; access: null }
  | { status: 'error'; access: null };

export type ShowAccessMode = 'private' | 'limited' | 'public';

/** A `limited` audience is a heterogeneous set: an email, an Organization group,
 *  or the page's own Organization. The three kinds are peers — any hit admits a
 *  read-only `/p` visitor, and none of them supersedes or dedupes another. */
export type ShowAccessEntryKind = 'email' | 'group' | 'organization';

export type ShowAccessEntry = {
  kind: ShowAccessEntryKind;
  value: string;
};

export type ShowAccess = {
  page_id: string;
  access_mode: ShowAccessMode;
  share_id: string | null;
  revision: number;
  /** Pre-A1 backends only report the email audience. Absent `access_entries`,
   *  this list is read as the email entries so email sharing keeps working. */
  normalized_emails: string[];
  access_entries?: ShowAccessEntry[];
};

export type ShowAccessSettingsResult = {
  show_access: ShowAccess;
};

export type ShowAccessApplyRequest = {
  expected_revision: number;
  target_access_mode: ShowAccessMode;
  target_share_id: string | null;
  /** Heterogeneous audience. Optional until A1/A2 land the apply contract: the
   *  current endpoint requires an exact five-key payload, so the wire form
   *  omits this until that backend accepts it. */
  target_entries?: ShowAccessEntry[];
  /** Email projection of `target_entries`. Until A1/A2 land, this is the only
   *  audience the current endpoint applies. */
  target_emails: string[];
};

/** Wire payload for `/access-settings/apply`. Until A1/A2 land, the route
 *  requires an exact five-key set and 400s on any extra field, so the
 *  heterogeneous audience is held locally and only the email projection is
 *  sent. Once those lanes land, this helper should start including
 *  `target_entries` again. */
export function showAccessApplyPayload(
  expectedRevision: number,
  mode: ShowAccessMode,
  shareId: string | null,
  entries: ShowAccessEntry[],
): ShowAccessApplyRequest {
  return {
    expected_revision: expectedRevision,
    target_access_mode: mode,
    target_share_id: shareId,
    target_emails: showAccessTargetEmails(mode, entries),
  };
}

export type ShowAccessApplyResult = {
  status: 'applied' | 'no_change' | 'conflict' | 'share_id_taken' | 'invalid';
  show_access: ShowAccess;
};

export const SHOW_ACCESS_ENTRY_MAX_COUNT = 64;
const SHOW_ACCESS_EMAIL_PATTERN = /^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$/;
const ASCII_SURROUNDING_WHITESPACE = /^[\t\n\f\r\v ]+|[\t\n\f\r\v ]+$/g;

export function normalizeShowAccessEmail(raw: string): string | null {
  const normalized = raw
    .replace(ASCII_SURROUNDING_WHITESPACE, '')
    .replace(/[A-Z]/g, (value) => value.toLowerCase());
  return normalized.length <= 320 && SHOW_ACCESS_EMAIL_PATTERN.test(normalized)
    ? normalized
    : null;
}

export function normalizeShowAccessEmails(emails: string[]): string[] | null {
  if (emails.length > SHOW_ACCESS_ENTRY_MAX_COUNT) return null;
  const normalized = emails.map(normalizeShowAccessEmail);
  if (normalized.some((email) => email === null)) return null;
  return [...new Set(normalized as string[])].sort();
}

const ENTRY_KIND_RANK: Record<ShowAccessEntryKind, number> = {
  organization: 0,
  group: 1,
  email: 2,
};

export function showAccessEntryKey(entry: ShowAccessEntry): string {
  return `${entry.kind}:${entry.value}`;
}

export function showAccessEntriesKey(entries: ShowAccessEntry[]): string {
  return entries.map(showAccessEntryKey).join('\u0000');
}

/** Canonical audience order: the Organization entry, then groups, then emails.
 *  The wire form has to be stable so a draft comparison never reports a change
 *  the user did not make. */
export function canonicalShowAccessEntries(entries: ShowAccessEntry[]): ShowAccessEntry[] {
  const seen = new Set<string>();
  const unique: ShowAccessEntry[] = [];
  for (const entry of entries) {
    const key = showAccessEntryKey(entry);
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push({ kind: entry.kind, value: entry.value });
  }
  return unique.sort((left, right) => (
    ENTRY_KIND_RANK[left.kind] - ENTRY_KIND_RANK[right.kind]
    || left.value.localeCompare(right.value)
  ));
}

export function showAccessEntriesOf(saved: ShowAccess): ShowAccessEntry[] {
  return canonicalShowAccessEntries(
    saved.access_entries
      ?? saved.normalized_emails.map((value) => ({ kind: 'email' as const, value })),
  );
}

/** Adds one entry. At most one Organization entry can exist, so a second one
 *  replaces the first instead of accumulating a set the backend would reject. */
export function withShowAccessEntry(
  entries: ShowAccessEntry[],
  entry: ShowAccessEntry,
): ShowAccessEntry[] {
  const kept = entry.kind === 'organization'
    ? entries.filter((current) => current.kind !== 'organization')
    : entries;
  return canonicalShowAccessEntries([...kept, entry]);
}

export function withoutShowAccessEntry(
  entries: ShowAccessEntry[],
  entry: ShowAccessEntry,
): ShowAccessEntry[] {
  const removed = showAccessEntryKey(entry);
  return canonicalShowAccessEntries(
    entries.filter((current) => showAccessEntryKey(current) !== removed),
  );
}

export function showAccessTargetEntries(
  mode: ShowAccessMode,
  entries: ShowAccessEntry[],
): ShowAccessEntry[] {
  return mode === 'limited' ? canonicalShowAccessEntries(entries) : [];
}

export function showAccessTargetEmails(
  mode: ShowAccessMode,
  entries: ShowAccessEntry[],
): string[] {
  return showAccessTargetEntries(mode, entries)
    .filter((entry) => entry.kind === 'email')
    .map((entry) => entry.value);
}

/** The Organization directory the audience combobox searches. `null` means this
 *  Avibe has no Organization (Personal), which is what hides the Organization
 *  toggle and group search — Personal pages can only list emails. */
export type ShowAccessDirectory = {
  organization_id: string;
  organization_name: string;
  groups: { id: string; name: string }[];
  emails: string[];
};

export function showAccessDirectoryOf(
  permissions: PermissionsResponse,
): ShowAccessDirectory | null {
  const organization = permissions.projection.instance.organization;
  if (!organization) return null;
  return {
    organization_id: organization.id,
    organization_name: organization.name,
    groups: permissions.projection.directory.groups
      .filter((group) => group.archived_at === null)
      .map((group) => ({ id: group.id, name: group.name })),
    emails: permissions.projection.directory.members
      .map((member) => normalizeShowAccessEmail(member.email))
      .filter((email): email is string => email !== null),
  };
}

export type ShowAccessSuggestion = {
  kind: 'group' | 'email';
  value: string;
  label: string;
};

export const SHOW_ACCESS_SUGGESTION_LIMIT = 8;
const SHOW_ACCESS_GROUP_SUGGESTION_LIMIT = 4;

/** Search over the Organization directory. Groups keep the first slots so a
 *  large member list can never hide them; `truncated` lets the UI say results
 *  were dropped instead of silently showing a partial list. */
export function showAccessSuggestions(
  directory: ShowAccessDirectory | null,
  query: string,
  selected: ShowAccessEntry[],
): { suggestions: ShowAccessSuggestion[]; truncated: boolean } {
  if (!directory) return { suggestions: [], truncated: false };
  const taken = new Set(selected.map(showAccessEntryKey));
  const needle = query.trim().toLowerCase();
  const matches = (...fields: string[]) => (
    needle.length === 0 || fields.some((field) => field.toLowerCase().includes(needle))
  );
  const groups: ShowAccessSuggestion[] = directory.groups
    .filter((group) => !taken.has(`group:${group.id}`) && matches(group.name, group.id))
    .map((group) => ({ kind: 'group', value: group.id, label: group.name }));
  const people: ShowAccessSuggestion[] = directory.emails
    .filter((email) => !taken.has(`email:${email}`) && matches(email))
    .map((email) => ({ kind: 'email', value: email, label: email }));
  const shownGroups = groups.slice(0, SHOW_ACCESS_GROUP_SUGGESTION_LIMIT);
  const shownPeople = people.slice(0, SHOW_ACCESS_SUGGESTION_LIMIT - shownGroups.length);
  return {
    suggestions: [...shownGroups, ...shownPeople],
    truncated: shownGroups.length < groups.length || shownPeople.length < people.length,
  };
}

export function showAccessDraftChanged(
  saved: ShowAccess,
  mode: ShowAccessMode,
  shareId: string | null,
  entries: ShowAccessEntry[],
): boolean {
  return saved.access_mode !== mode
    || saved.share_id !== shareId
    || showAccessEntriesKey(showAccessEntriesOf(saved))
      !== showAccessEntriesKey(showAccessTargetEntries(mode, entries));
}

/** True when the five-key apply payload would differ. Group and Organization
 *  entries are invisible to the current endpoint, so a local-only audience
 *  change must not be sent (it would 400 on `target_entries`, or a successful
 *  email-only round-trip would wipe those rows on adopt). */
export function showAccessWireChanged(
  saved: ShowAccess,
  mode: ShowAccessMode,
  shareId: string | null,
  entries: ShowAccessEntry[],
): boolean {
  return saved.access_mode !== mode
    || saved.share_id !== shareId
    || showAccessEntriesKey(
      saved.normalized_emails.map((value) => ({ kind: 'email' as const, value })),
    ) !== showAccessEntriesKey(
      showAccessTargetEmails(mode, entries).map((value) => ({ kind: 'email' as const, value })),
    );
}

/** Re-homes group/Organization rows the current endpoint cannot store, on top
 *  of whatever email audience the server just acknowledged. */
export function showAccessWithLocalExtras(
  saved: ShowAccess,
  local: ShowAccessEntry[],
): ShowAccessEntry[] {
  return canonicalShowAccessEntries([
    ...local.filter((entry) => entry.kind !== 'email'),
    ...showAccessEntriesOf(saved).filter((entry) => entry.kind === 'email'),
  ]);
}

function isShowPageAccess(value: unknown): value is ShowPageAccess {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<ShowPageAccess>;
  return candidate.ok === true
    && (
      candidate.mode === 'unmanaged'
      || candidate.mode === 'personal'
      || candidate.mode === 'organization'
      || candidate.mode === 'organization_pending'
      || candidate.mode === 'configuration_unavailable'
    )
    && typeof candidate.ownership_status === 'string'
    && typeof candidate.can_use === 'boolean'
    && typeof candidate.can_manage === 'boolean'
    && typeof candidate.can_publish_public === 'boolean';
}

export function classifyShowPageAccessProbe(
  status: number,
  payload: unknown,
): ShowPageAccessProbe {
  if (status >= 200 && status < 300 && isShowPageAccess(payload)) {
    return { status: 'granted', access: payload };
  }
  if (status === 403 || status === 404) {
    return { status: 'denied', access: null };
  }
  return { status: 'error', access: null };
}

export function showPageRestoreAccessDecision(
  canManageInstance: boolean,
  probe: ShowPageAccessProbe | null,
): 'allow' | 'deny' | 'wait' {
  if (canManageInstance) return 'allow';
  if (!probe || probe.status === 'error') return 'wait';
  if (probe.status === 'denied') return 'deny';
  return probe.access.can_use ? 'allow' : 'deny';
}

export function showPageHeaderAccess(
  canManageInstance: boolean,
  access: ShowPageAccess | null,
): { canOpen: boolean; canManage: boolean } {
  return {
    canOpen: canManageInstance || access?.can_use === true,
    canManage: canManageInstance || access?.can_manage === true,
  };
}
