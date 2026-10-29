import type { TFunction } from 'i18next';

type AgentLike = { name: string; display_name?: string | null; model?: string | null; reasoning_effort?: string | null };
type ScopeRouting = { agent_name?: string | null; model?: string | null; reasoning_effort?: string | null } | null | undefined;

/**
 * The route a channel, topic, or DM user actually runs, as one line for its settings row.
 *
 * A scope's own model and effort win over its Agent's, and a scope that names
 * none of them runs the default Agent as it is, which the line marks as the
 * default — the same reading the route picker shows when the row is expanded.
 */
export function scopeRouteSummary(
  routing: ScopeRouting,
  agentsByName: Record<string, AgentLike>,
  defaultAgentName: string | null | undefined,
  t: TFunction,
): string {
  const inherited = !routing?.agent_name && !routing?.model && !routing?.reasoning_effort;
  const agentName = routing?.agent_name || defaultAgentName || '';
  const agent = agentsByName[agentName];
  const model = routing?.model || agent?.model || '';
  const effort = routing?.reasoning_effort || agent?.reasoning_effort || '';
  const route = [agent?.display_name || agentName, model].filter(Boolean).join('/');
  const parts = [
    inherited ? t('common.default') : '',
    route,
    effort ? t(`chat.picker.effortOptions.${effort}`, { defaultValue: effort }) : '',
  ];
  return parts.filter(Boolean).join(' · ');
}
