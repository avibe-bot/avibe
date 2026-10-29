// One settings edit on a channel, Telegram topic, or bound DM user, as the
// PATCH /api/settings/scope contract takes it: only the fields the edit
// changed, so a page holding an older copy of a scope never writes back what
// it did not touch.

export type ScopeKind = 'channel' | 'thread' | 'user';

export const CHANNEL_SCOPE_FIELDS = [
  'enabled',
  'show_message_types',
  'custom_cwd',
  'require_mention',
  'require_bind',
] as const;

export const USER_SCOPE_FIELDS = ['display_name', 'enabled', 'show_message_types', 'custom_cwd'] as const;

type Routing = Record<string, string | null | undefined>;

export interface ScopeSettingsChange {
  changes: Record<string, unknown>;
  /** The Agent the edited view's routing was based on; sent with any routing change. */
  expected_agent_name?: string | null;
}

const same = (left: unknown, right: unknown) => JSON.stringify(left ?? null) === JSON.stringify(right ?? null);

/** The fields `next` changes on `base`, or null when it changes nothing. */
export function scopeSettingsChange<Row extends { routing?: object | null }>(
  base: Row,
  next: Row,
  fields: readonly string[],
): ScopeSettingsChange | null {
  const changes: Record<string, unknown> = {};
  const baseFields = base as Record<string, unknown>;
  const nextFields = next as Record<string, unknown>;
  for (const field of fields) {
    if (!same(baseFields[field], nextFields[field])) changes[field] = nextFields[field] ?? null;
  }
  const before = (base.routing || {}) as Routing;
  const after = (next.routing || {}) as Routing;
  const routing: Record<string, string | null> = {};
  for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
    if (!same(before[key], after[key])) routing[key] = after[key] ?? null;
  }
  const result: ScopeSettingsChange = { changes };
  if (Object.keys(routing).length) {
    changes.routing = routing;
    result.expected_agent_name = before.agent_name ?? null;
  }
  return Object.keys(changes).length ? result : null;
}
