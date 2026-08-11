import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { classifyChainLink, SOURCE_STATE_CLASSIFICATION } from './sourceStateClassification';
import {
  CHAIN_HEALTHS,
  CHAIN_UNAVAILABLE_REASONS,
  SOURCE_STATUSES,
  type AgentChainLink,
} from './types';

const CONTRACTS = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../../../..',
  'docs/plans/model-hub-contracts',
);

describe('source state classification authority', () => {
  it('mirrors the closed Source and AgentChain vocabularies from their schemas', () => {
    const source = JSON.parse(readFileSync(resolve(CONTRACTS, 'source.schema.json'), 'utf8'));
    const chain = JSON.parse(readFileSync(resolve(CONTRACTS, 'agent-chain.schema.json'), 'utf8'));
    const link = chain.properties.chain.items.properties;
    const reasons = link.reason.anyOf.flatMap((branch: { const?: unknown; enum?: unknown[] }) => (
      branch.const === undefined ? branch.enum ?? [] : [branch.const]
    )).filter((value: unknown): value is string => typeof value === 'string');

    expect(new Set(SOURCE_STATUSES)).toEqual(new Set(source.properties.state.properties.status.enum));
    expect(new Set(CHAIN_HEALTHS)).toEqual(new Set(link.health.enum));
    expect(new Set(CHAIN_UNAVAILABLE_REASONS)).toEqual(new Set(reasons));
    expect(new Set(Object.keys(SOURCE_STATE_CLASSIFICATION.sourceStatus))).toEqual(new Set(SOURCE_STATUSES));
    expect(new Set(Object.keys(SOURCE_STATE_CLASSIFICATION.health))).toEqual(new Set(CHAIN_HEALTHS));
    expect(new Set(Object.keys(SOURCE_STATE_CLASSIFICATION.reason))).toEqual(new Set(CHAIN_UNAVAILABLE_REASONS));
  });

  it('separates timed recovery, process blockers, user action, and gone targets', () => {
    const link = (health: AgentChainLink['health'], reason: AgentChainLink['reason']): AgentChainLink => ({
      source_id: 'src_example00',
      model_id: 'model-a',
      channel: reason === 'native_cli_unavailable' ? 'native_cli' : 'hub',
      health,
      runnable: false,
      reason,
      retry_at: health === 'cooldown' || health === 'backoff' ? '2099-01-01T00:00:00Z' : null,
    });

    expect(classifyChainLink(link('cooldown', null))).toBe('self_healing');
    expect(classifyChainLink(link('backoff', 'models.source.backoff.connection_failed'))).toBe('self_healing');
    expect(classifyChainLink(link('backoff', 'native_cli_unavailable'))).toBe('process_blocked');
    expect(classifyChainLink(link('needs_action', 'models.source.needs_action.oauth_expired'))).toBe('needs_user');
    expect(classifyChainLink(link('healthy', 'source_missing'))).toBe('gone');
  });

  it('keeps takeover and relation projection as consumers, not competing tables', () => {
    const takeover = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), 'takeover.ts'), 'utf8');
    const relations = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), 'supplyRelations.ts'), 'utf8');
    expect(takeover).toContain('classifyChainLink(head)');
    expect(relations).toContain('classifyChainLink(link)');
    expect(`${takeover}\n${relations}`).not.toMatch(/SELF_HEALING_HEAD|RECOVERABLE_PROCESS_REASON/);
  });
});
