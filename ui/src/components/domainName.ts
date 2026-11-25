// The rules for a domain's name, wherever one is chosen (DomainField: binding this computer;
// changing its domain).
/** What the field keeps of what was typed: no surrounding spaces, lowercase. The same everywhere a
 *  domain is chosen (binding this computer; changing its domain). */
export const cleanDomainName = (typed: string) => typed.trim().toLowerCase();
/** A name avibe.bot can take: 2–46 lowercase letters, digits and hyphens, not starting or ending
 *  with a hyphen, and no `--` in the third and fourth places. Anything else is never checked. */
export const validDomainName = (name: string) => /^[a-z0-9][a-z0-9-]{0,44}[a-z0-9]$/.test(name) && name.slice(2, 4) !== '--';
