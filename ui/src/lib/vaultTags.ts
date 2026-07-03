/**
 * Tag helpers for the Vaults UI.
 *
 * Vaults keep one global secret namespace plus tags — there are no groups. Skill
 * association is a reserved tag (`skill:<name>`), so a secret's flat `tags` array
 * can mix normal tags (`prod`, `deploy`) with skill tags (`skill:github-release`).
 * The UI renders skills in their own section but stores and selects on tags only,
 * matching the backend (`storage/vault_service.py` `skill_tag` / `SKILL_TAG_PREFIX`).
 */

export const SKILL_TAG_PREFIX = 'skill:';

/** True for a reserved skill tag (`skill:<name>`). */
export function isSkillTag(tag: string): boolean {
  return tag.startsWith(SKILL_TAG_PREFIX);
}

/** Strip the `skill:` prefix to the bare skill name; a normal tag is returned as-is. */
export function skillName(tag: string): string {
  return isSkillTag(tag) ? tag.slice(SKILL_TAG_PREFIX.length) : tag;
}

/** Wrap a bare skill name into its reserved tag form; already-prefixed input is left alone. */
export function toSkillTag(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return '';
  return isSkillTag(trimmed) ? trimmed : `${SKILL_TAG_PREFIX}${trimmed}`;
}

/**
 * Normalize a tag entry the way the backend does (`_normalize_tag`): trimmed,
 * non-empty, no interior whitespace. Also rejects the reserved `skill:` prefix — skills
 * belong in the dedicated skills input, which owns the prefix and the `links.skills` bridge.
 * Accepting `skill:` here would store a skill tag with no matching skill link (rendered/
 * filtered as a skill but invisible to skill-scoped access on the pre-refactor backend).
 * Returns `null` to reject — the shape the {@link TagInput} `normalize` prop expects.
 */
export function normalizeTagEntry(raw: string): string | null {
  const tag = raw.trim();
  if (!tag) return null;
  if (/\s/.test(tag)) return null;
  if (isSkillTag(tag)) return null;
  return tag;
}

/**
 * Normalize a skill entry: a bare skill name with no whitespace and without the
 * reserved prefix (the prefix is added on submit). Rejects a raw `skill:` entry so
 * the two inputs don't double-prefix.
 */
export function normalizeSkillEntry(raw: string): string | null {
  const name = skillName(raw.trim());
  return normalizeTagEntry(name);
}

const dedupe = (values: string[]): string[] => [...new Set(values)];

/**
 * Split a secret's flat tag list into display buckets: normal `tags` and bare
 * `skills` (prefix stripped). Order is preserved; duplicates collapse.
 */
export function partitionTags(tags: readonly string[] | null | undefined): { tags: string[]; skills: string[] } {
  const plain: string[] = [];
  const skills: string[] = [];
  for (const raw of tags ?? []) {
    if (typeof raw !== 'string') continue;
    const tag = raw.trim();
    if (!tag) continue;
    if (isSkillTag(tag)) skills.push(skillName(tag));
    else plain.push(tag);
  }
  return { tags: dedupe(plain), skills: dedupe(skills) };
}

/**
 * Merge normal tags and bare skill names back into the flat, deduped tag array the
 * backend stores. Skill names are re-prefixed; empties are dropped.
 */
export function mergeTags(tags: readonly string[], skills: readonly string[]): string[] {
  const normalTags = tags.map((tag) => tag.trim()).filter(Boolean);
  const skillTags = skills.map(toSkillTag).filter(Boolean);
  return dedupe([...normalTags, ...skillTags]);
}
