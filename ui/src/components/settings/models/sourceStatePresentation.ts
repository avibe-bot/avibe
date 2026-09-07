import type { AgentBackend, AdoptedBy, NeedsActionDetailKey, SourceState, SourceStatus } from './types';

export type SourceStateSurface = 'card' | 'detail';

export type SourceStatePresentation = {
  key: string | null;
  values?: Record<string, string>;
  /**
   * Copy that explains the label in place, for a reading whose plain meaning is
   * not the meaning a first-time user takes from it. Both halves are named here
   * because the affordance carrying them is a control: the body is what it says
   * and the label is what it is called, and neither is a string a consumer may
   * invent at the keyboard.
   */
  hint?: { labelKey: string; bodyKey: string };
  textClass: string;
  dotClass: string;
};

/**
 * Keep the cached Source adoption projection readable while removing backends
 * that the authoritative Agent projection has already switched to direct mode.
 * An omitted active-backend set means the Agent read is not authoritative yet.
 */
export const activeSourceAdoption = (
  adoptedBy: AdoptedBy[] | undefined,
  activeBackends?: ReadonlySet<AgentBackend>,
): AdoptedBy[] | undefined => activeBackends && adoptedBy
  ? adoptedBy.filter(({ backend }) => activeBackends.has(backend))
  : adoptedBy;

/**
 * A healthy source that no route currently reaches. It is the state a source
 * lands in the moment it is added, and 备用 next to a key just created reads as
 * "the add failed" — so the authority that names the state also names the
 * sentence that explains it, and every surface rendering the label can reach
 * the same one. `active` without an adopting backend is the same reading and
 * shares this presentation rather than restating it.
 */
const STANDBY: SourceStatePresentation = {
  key: 'settings.models.upstream.state.standby',
  hint: {
    labelKey: 'settings.models.sourceDetail.status.standbyHintLabel',
    bodyKey: 'settings.models.sourceDetail.status.standbyHint',
  },
  textClass: 'text-muted',
  dotClass: 'bg-muted',
};

const NEEDS_ACTION_KEY: Readonly<Record<NeedsActionDetailKey, string>> = {
  'models.source.needs_action.oauth_expired': 'settings.models.sourceDetail.status.needsAction.oauthExpired',
  'models.source.needs_action.balance_exhausted': 'settings.models.sourceDetail.status.needsAction.balanceExhausted',
  'models.source.needs_action.credential_revoked': 'settings.models.sourceDetail.status.needsAction.credentialRevoked',
  'models.source.needs_action.account_banned': 'settings.models.sourceDetail.status.needsAction.accountBanned',
};

type Rule = (state: SourceState, surface: SourceStateSurface, locale: string, now: number) => SourceStatePresentation;

const STATUS_RULES: Readonly<Record<SourceStatus, Rule>> = {
  active: () => STANDBY,
  standby: () => STANDBY,
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
    textClass: 'text-destructive-ink',
    dotClass: 'bg-destructive',
  }),
  error: () => ({
    key: 'settings.models.sourceDetail.status.error',
    textClass: 'text-destructive-ink',
    dotClass: 'bg-destructive',
  }),
};

export const sourceStatePresentation = (
  state: SourceState,
  surface: SourceStateSurface,
  locale: string,
  now: number = Date.now(),
  adoption: { known: boolean; backends: string[]; native: boolean; verificationPending?: boolean } = { known: false, backends: [], native: false },
): SourceStatePresentation => {
  if (adoption.verificationPending && (state.status === 'active' || state.status === 'standby')) {
    return {
      key: 'settings.models.sourceDetail.status.unverified',
      textClass: 'model-hub-ink-gold',
      dotClass: 'bg-gold',
    };
  }
  if (state.status === 'active' && !adoption.known) {
    return { key: null, textClass: 'text-muted', dotClass: 'bg-muted' };
  }
  if ((state.status === 'active' || state.status === 'standby') && adoption.backends.length > 0) {
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
      textClass: adoption.native ? 'text-cyan-ink' : 'model-hub-ink-mint',
      dotClass: adoption.native ? 'bg-cyan' : 'bg-mint',
    };
  }
  return STATUS_RULES[state.status](state, surface, locale, now);
};
