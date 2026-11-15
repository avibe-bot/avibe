// Whether a new secret's name is free, checked before anything is made or opened (owner, 2026-11-15:
// a seed-phrase wallet must not get as far as its words before learning the name is taken). The
// server stays the authority: it refuses a taken name again at save, and the form handles that too.

/** Why a name can't be used: another secret has it, or one differs from it only in letter case. */
export type NameIssue = { kind: 'taken' } | { kind: 'case'; existing: string };

/** The issue with `name` among the existing secret names, if any (names are case-sensitive, but two
 *  may not differ only in case). */
export function nameIssueAmong(name: string, existing: readonly string[]): NameIssue | null {
  if (!name) return null;
  if (existing.includes(name)) return { kind: 'taken' };
  const lower = name.toLowerCase();
  const other = existing.find((candidate) => candidate.toLowerCase() === lower);
  return other ? { kind: 'case', existing: other } : null;
}

// The Add key dialog remembers the tier last chosen in it for this browser tab's session, so reopening
// it after a failed protected save does not quietly start over on Standard (owner, 2026-11-15).
const TIER_KEY = 'avibe.vault.addTier';
export const rememberAddTier = (tier: 'standard' | 'protected') => {
  try {
    sessionStorage.setItem(TIER_KEY, tier);
  } catch {
    // Storage unavailable: the dialog opens on its default tier.
  }
};
export const rememberedAddTier = (): 'standard' | 'protected' | null => {
  try {
    const tier = sessionStorage.getItem(TIER_KEY);
    return tier === 'standard' || tier === 'protected' ? tier : null;
  } catch {
    return null;
  }
};
