import type {
  AgentChainLink,
  ChainHealth,
  ChainUnavailableReason,
  SourceStatus,
} from './types';

export type SourceStateClass = 'self_healing' | 'process_blocked' | 'needs_user' | 'gone';

/** One exhaustive state authority for persisted Source status and live chain blockers. */
export const SOURCE_STATE_CLASSIFICATION = {
  sourceStatus: {
    active: null,
    standby: null,
    cooldown: 'self_healing',
    needs_action: 'needs_user',
    error: 'needs_user',
  },
  health: {
    healthy: null,
    cooldown: 'self_healing',
    backoff: 'self_healing',
    needs_action: 'needs_user',
    error: 'needs_user',
  },
  reason: {
    native_cli_unavailable: 'process_blocked',
    'models.source.backoff.connection_failed': 'self_healing',
    source_missing: 'gone',
    model_unsupported: 'gone',
    'models.source.needs_action.oauth_expired': 'needs_user',
    'models.source.needs_action.balance_exhausted': 'needs_user',
    'models.source.needs_action.credential_revoked': 'needs_user',
    'models.source.needs_action.account_banned': 'needs_user',
    'models.source.error.unclassified': 'needs_user',
  },
} as const satisfies {
  sourceStatus: Readonly<Record<SourceStatus, SourceStateClass | null>>;
  health: Readonly<Record<ChainHealth, SourceStateClass | null>>;
  reason: Readonly<Record<ChainUnavailableReason, SourceStateClass>>;
};

export const classifySourceStatus = (status: SourceStatus): SourceStateClass | null =>
  SOURCE_STATE_CLASSIFICATION.sourceStatus[status];

export const classifyChainLink = (link: AgentChainLink): SourceStateClass | null =>
  link.reason === null
    ? SOURCE_STATE_CLASSIFICATION.health[link.health]
    : SOURCE_STATE_CLASSIFICATION.reason[link.reason];
