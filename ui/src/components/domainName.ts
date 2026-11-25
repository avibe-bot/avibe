// The rules for a domain's name, wherever one is chosen (DomainField: binding this computer;
// changing its domain).
/** What the field keeps of what was typed: no surrounding spaces, lowercase. The same everywhere a
 *  domain is chosen (binding this computer; changing its domain). */
export const cleanDomainName = (typed: string) => typed.trim().toLowerCase();

/** Why avibe.bot cannot take a name, one reason at a time, in the order the field says them
 *  (product, 2026-11-25): characters, then length, then where the hyphens are. */
export type DomainNameProblem = 'characters' | 'short' | 'long' | 'hyphenEnds' | 'doubleHyphen';

export const DOMAIN_NAME_MIN = 2;
export const DOMAIN_NAME_MAX = 46;

/** The first rule `name` breaks, or null when avibe.bot can take it. An empty name breaks none:
 *  the field says nothing yet, and nothing can be pressed. */
export function domainNameProblem(name: string): DomainNameProblem | null {
  if (!name) return null;
  if (!/^[a-z0-9-]+$/.test(name)) return 'characters';
  if (name.length < DOMAIN_NAME_MIN) return 'short';
  if (name.length > DOMAIN_NAME_MAX) return 'long';
  if (name.startsWith('-') || name.endsWith('-')) return 'hyphenEnds';
  // The shape reserved for internationalized names (`xn--`).
  if (name.slice(2, 4) === '--') return 'doubleHyphen';
  return null;
}

/** A name avibe.bot can take: 2–46 lowercase letters, digits and hyphens, not starting or ending
 *  with a hyphen, and no `--` in the third and fourth places. Anything else is never checked. */
export const validDomainName = (name: string) => Boolean(name) && domainNameProblem(name) === null;
