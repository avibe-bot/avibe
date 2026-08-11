import type { NeedsActionDetailKey, SourceState, SourceStatus } from './types';

export type SourceStateSurface = 'card' | 'detail';

export type SourceStatePresentation = {
  key: string | null;
  values?: Record<string, string>;
  textClass: string;
  dotClass: string;
};

const NEEDS_ACTION_KEY: Readonly<Record<NeedsActionDetailKey, string>> = {
  'models.source.needs_action.oauth_expired': 'settings.models.sourceDetail.status.needsAction.oauthExpired',
  'models.source.needs_action.balance_exhausted': 'settings.models.sourceDetail.status.needsAction.balanceExhausted',
  'models.source.needs_action.credential_revoked': 'settings.models.sourceDetail.status.needsAction.credentialRevoked',
  'models.source.needs_action.account_banned': 'settings.models.sourceDetail.status.needsAction.accountBanned',
};

type Rule = (state: SourceState, surface: SourceStateSurface, locale: string, now: number) => SourceStatePresentation;

const STATUS_RULES: Readonly<Record<SourceStatus, Rule>> = {
  active: () => ({
    // The persisted Source projection does not carry adopted_by (G-20), so an
    // active credential proves health but not that a configured Route uses it.
    key: 'settings.models.upstream.state.standby',
    textClass: 'text-muted',
    dotClass: 'bg-muted',
  }),
  standby: () => ({
    key: 'settings.models.upstream.state.standby',
    textClass: 'text-muted',
    dotClass: 'bg-muted',
  }),
  cooldown: (state, _surface, locale, now) => {
    const retryAt = state.retry_at ? new Date(state.retry_at).getTime() : Number.NaN;
    const remainingMinutes = Math.ceil((retryAt - now) / 60_000);
    if (!Number.isFinite(retryAt) || remainingMinutes <= 0) {
      return {
        key: 'settings.models.upstream.state.unavailableDue',
        textClass: 'model-hub-ink-gold',
        dotClass: 'bg-gold',
      };
    }
    return {
      key: 'settings.models.upstream.state.unavailableRetry',
      values: {
        delay: new Intl.NumberFormat(locale, { style: 'unit', unit: 'minute', unitDisplay: 'long' }).format(remainingMinutes),
      },
      textClass: 'model-hub-ink-gold',
      dotClass: 'bg-gold',
    };
  },
  needs_action: (state) => ({
    key: state.detail_key
      ? NEEDS_ACTION_KEY[state.detail_key as NeedsActionDetailKey] ?? state.detail_key
      : 'settings.models.state.needs_action',
    textClass: 'text-destructive',
    dotClass: 'bg-destructive',
  }),
  error: () => ({
    key: 'settings.models.sourceDetail.status.error',
    textClass: 'text-destructive',
    dotClass: 'bg-destructive',
  }),
};

export const sourceStatePresentation = (
  state: SourceState,
  surface: SourceStateSurface,
  locale: string,
  now: number = Date.now(),
  adoption: { backends: string[]; native: boolean } = { backends: [], native: false },
): SourceStatePresentation => {
  if (state.status === 'active' && adoption.backends.length > 0) {
    if (surface === 'detail') {
      return {
        key: 'settings.models.sourceDetail.status.inUse',
        textClass: 'model-hub-ink-mint',
        dotClass: 'bg-mint',
      };
    }
    return {
      key: adoption.native
        ? 'settings.models.upstream.state.supplyingNative'
        : 'settings.models.upstream.state.supplying',
      values: adoption.native
        ? { backend: adoption.backends[0] }
        : { backends: adoption.backends.join(locale.startsWith('zh') ? '、' : ', ') },
      textClass: adoption.native ? 'text-cyan' : 'model-hub-ink-mint',
      dotClass: adoption.native ? 'bg-cyan' : 'bg-mint',
    };
  }
  return STATUS_RULES[state.status](state, surface, locale, now);
};
