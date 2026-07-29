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
