// Rendering an i18n key the SERVER chose, safely.
//
// Two kinds of key reach this page from the backend. Closed vocabularies —
// `SourceState.detail_key`, `ProbeResult.error` — are declared member-by-member
// in the contract schemas, so contractLocaleKeys.test.ts can enumerate them and
// fail the build when a bundle is missing one. Open ones cannot be: the
// oauth-flow's `instructions_key` / `error_key` and a migration item's
// `notes_key` are described as free-form runtime-declared keys, so no test can
// know their membership ahead of time and any of them can be absent from a bundle
// at runtime — a new adapter rev is enough.
//
// i18next's default for a missing key is the key itself, so the honest failure
// mode without this helper is a machine string ("models.migration.keep_native.
// sanctioned") rendered to a user as if it were a sentence. This turns that into
// the generic copy the surrounding UI already has for the same situation.
//
// Not in format.ts on purpose: that module is deliberately i18n-free (its callers
// wrap what it returns), and this one has to take `t`.
import type { TFunction } from 'i18next';

/**
 * Translate a server-declared key, falling back to `fallbackKey` when the bundle
 * has no entry for it. Returns null when neither resolves — omit `fallbackKey`
 * for optional copy, where rendering nothing is the honest degradation.
 */
export const serverText = (
  t: TFunction,
  key: string | null | undefined,
  fallbackKey?: string,
): string | null => {
  if (key) {
    // `defaultValue: ''` rather than the fallback text itself: i18next returns
    // the default for a missing key, and an empty one lets us tell "missing" from
    // "translated to something" without string-comparing against the key.
    const translated = t(key, { defaultValue: '' }) as string;
    if (translated) return translated;
  }
  if (!fallbackKey) return null;
  const generic = t(fallbackKey, { defaultValue: '' }) as string;
  return generic || null;
};

/**
 * Which journey a terminal OAuth failure belongs to. Required, not optional: the
 * SAME server code means two different things to the user depending on it, and a
 * default would silently pick one.
 */
export type OAuthJourney = 'connect' | 'reauth';

/**
 * The failure codes raised while MATERIALIZING the terminal flow, not while
 * authorizing it (api.md → POST /sources errors). The vendor said yes; what came
 * after it didn't.
 *
 * Membership is 「this code can ONLY arise after terminal success」, and that is
 * what disqualified `engine_down`. It is the engine-outage catch-all: every
 * `_oauth_call` maps `EngineUnavailableError` — and any unexpected exception —
 * onto it, and `_oauth_status` is one of its callers, so a plain outage during
 * polling produces it while the flow is still pending. Reading it as
 * materialization made a reauth announce 「已重新登录，但这个来源还是不可用」 for a
 * sign-in that had not happened, which is the one claim the user cannot check.
 *
 * It falls through to `generic` (「连接失败，请重试」) rather than to a line naming
 * the engine, because the same code is also raised for
 * `NativeOAuthUnavailableError`: 「中枢没有响应」 would be false on the native
 * channel, the same false-on-the-channel-reading-it problem the copy in this
 * lane keeps hitting. Generic states the outage and asks for a retry, which is
 * true whichever phase it interrupted.
 */
const MATERIALIZE_CODES = ['discovery_failed', 'migration_item_conflict'];

/**
 * A terminal-response failure code → the copy for it, for THIS journey.
 *
 * Keeping 「授权没成」 apart from 「授权成了，后面没成」 is why the second branch
 * exists at all. The journey argument is why it is right: a create that gets there
 * has no Source yet, so 「couldn't create the source」 is the whole truth — but a
 * reauth is repairing a source that already exists, and `_materialize_reauth`
 * clears it and marks it unavailable before answering `discovery_failed`. Telling
 * that user their source could not be CREATED names the wrong object and hides the
 * thing that just happened to the one they have.
 */
export const oauthFailureKey = (code: string | undefined, journey: OAuthJourney): string =>
  code && MATERIALIZE_CODES.includes(code)
    ? journey === 'reauth'
      ? 'settings.models.oauth.error.finalizeReauth'
      : 'settings.models.oauth.error.finalize'
    : 'settings.models.oauth.error.generic';

/** Start-route failures have not reached provider authorization yet. */
export const NATIVE_SUBSCRIPTION_EXISTS_FAILURE = 'modelHub.errors.native_subscription_exists';
export const NATIVE_LOGIN_IN_PROGRESS_FAILURE = 'modelHub.errors.native_login_in_progress';

export const oauthStartFailureKey = (code: string | undefined): string =>
  code === NATIVE_SUBSCRIPTION_EXISTS_FAILURE
    ? 'settings.models.addSub.error.alreadyBound'
    : code === NATIVE_LOGIN_IN_PROGRESS_FAILURE
      ? 'settings.models.addSub.error.loginInProgress'
    : code === 'engine_down' || code === 'modelHub.errors.engine_down'
      ? 'settings.models.addSub.error.engineDown'
      : 'settings.models.addSub.error.startFailed';
