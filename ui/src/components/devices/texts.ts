// The sentences the Devices pages choose by a code the instance answered: why a claim, a rename or
// a new binding did not go through. One sentence per code; a code this page does not know is said
// with the code, never as success.
import type { TFunction } from 'i18next';

import type { TranslationKey } from '@/i18n/types';
import { longDay } from './format';
import type { ClaimProblem, OptimizeState, RebindRefusal, RenameRefusal } from './types';

const problemKeys = {
  link_required: 'devices.claim.problem.linkRequired',
  organization: 'devices.claim.problem.organization',
  invalid_return: 'devices.claim.problem.invalidReturn',
  expired: 'devices.claim.problem.expired',
  declined: 'devices.claim.problem.declined',
  unreachable: 'devices.reach.unreachable',
  pairing_recovery_required: 'devices.claim.problem.recovering',
  fresh_claim_required: 'devices.claim.problem.freshClaim',
  name_taken: 'devices.claim.problem.nameTaken',
  binding_unavailable: 'devices.claim.problem.unexplained',
  paired_installation_loopback_required: 'devices.claim.problem.loopbackRequired',
  active_pairing_recent: 'devices.claim.problem.activeElsewhere',
  local_binding_changed: 'devices.claim.problem.bindingChanged',
  claim_in_progress: 'devices.claim.problem.inProgress',
} as const satisfies Record<Exclude<ClaimProblem['code'], 'rate_limited' | 'unknown'>, TranslationKey>;

/**
 * Why remote access was not turned on, in one sentence. An unknown code is said with its code.
 * `terminal`: this instance has the terminal's way of binding (`vibe remote rebind`); without it
 * the sentence does not send anyone to a command that is not there.
 */
export function claimProblemText(t: TFunction, problem: ClaimProblem, terminal = true): string {
  if (problem.code === 'unknown') return t('devices.claim.problem.unknown', { code: problem.raw });
  if (problem.code === 'link_required' && !terminal) return t('devices.claim.problem.linkRequiredHere');
  if (problem.code === 'rate_limited') {
    return problem.seconds
      ? t('devices.claim.problem.rateLimited', { count: problem.seconds })
      : t('devices.claim.problem.rateLimitedSoon');
  }
  return t(problemKeys[problem.code]);
}

const refusalKeys = {
  taken: 'devices.claim.taken',
  invalid_prefix: 'devices.rename.refused.invalidPrefix',
  same_address: 'devices.rename.refused.sameAddress',
  confirmation_expired: 'devices.rename.refused.confirmationExpired',
  operation_in_progress: 'devices.rename.refused.inProgress',
  address_changed: 'devices.rename.refused.addressChanged',
  pairing_invalid: 'devices.rename.refused.pairingInvalid',
  remote_access_configuration_mismatch: 'devices.rename.refused.configurationMismatch',
  rebind_required: 'devices.rename.refused.rebindRequired',
  unavailable: 'devices.rename.refused.unavailable',
  owner_required: 'devices.rename.refused.ownerRequired',
  rename_ineligible: 'devices.rename.refused.ineligible',
} as const satisfies Record<string, TranslationKey>;
/** Why the domain was not changed, in one sentence. An unknown code is said with its code. */
/** `rebindOffered`: this instance has binding again. Without it, the refusals whose way out that
 *  is say their reason only, and send nobody to an entry that is not there. */
export function renameRefusalText(t: TFunction, language: string, refusal: RenameRefusal, rebindOffered = true): string {
  if (!rebindOffered) {
    if (refusal.code === 'pairing_invalid') return t('devices.rename.refused.pairingInvalidPlain');
    if (refusal.code === 'remote_access_configuration_mismatch') return t('devices.rename.refused.configurationMismatchPlain');
    if (refusal.code === 'rebind_required') return t('devices.rename.refused.rebindRequiredPlain');
  }
  switch (refusal.code) {
    case 'unknown':
      return t('devices.rename.refused.unknown', { code: refusal.raw });
    case 'name_holding_limit_reached': {
      const date = refusal.earliestReleaseAt ? longDay(refusal.earliestReleaseAt, language) : null;
      if (typeof refusal.held === 'number') {
        return date
          ? t('devices.rename.refused.limitDated', { count: refusal.held, date })
          : t('devices.rename.refused.limit', { count: refusal.held });
      }
      return date ? t('devices.rename.refused.limitPlainDated', { date }) : t('devices.rename.refused.limitPlain');
    }
    default:
      return t(refusalKeys[refusal.code]);
  }
}

/** Why binding again did not go through. Shares the claim's sentences where the code is the same. */
export function rebindRefusalText(t: TFunction, refusal: RebindRefusal): string {
  switch (refusal.code) {
    case 'issuer_replay_refused':
      return t('devices.rebind.replayRefused');
    case 'operation_in_progress':
      return t('devices.rebind.busy');
    case 'confirmation_expired':
      return t('devices.rename.refused.confirmationExpired');
    case 'rate_limited':
      return claimProblemText(t, { code: 'rate_limited', seconds: refusal.seconds });
    case 'invalid_return':
      return t('devices.rebind.invalidReturn');
    case 'unknown':
      // A code this page does not know, the computer's own refusals among them: it says only what
      // it knows, not that avibe.bot refused or that nothing was bound.
      return t('devices.rebind.refusedUnknown', { code: refusal.raw });
    default:
      return claimProblemText(t, { code: refusal.code });
  }
}

/** The one line under 「优化线路」: why it did not start, else what it is waiting for. */
export function optimizeNote(t: TFunction, optimize: OptimizeState | undefined): string | undefined {
  if (!optimize || optimize.busy) return optimize?.problem ? t('devices.connection.optimizeFailed', { reason: optimize.problem }) : undefined;
  if (optimize.problem) return t('devices.connection.optimizeFailed', { reason: optimize.problem });
  const { wait } = optimize;
  if (wait?.kind === 'manual') return t('remoteAccess.manualRetryAt', { time: wait.time });
  if (optimize.started) return t('remoteAccess.optimizeStarted');
  if (wait?.kind === 'automatic') return t('remoteAccess.automaticRetryAt', { time: wait.time });
  return wait?.kind === 'due' ? t('remoteAccess.automaticRetryDue') : undefined;
}
