import { describe, expect, it } from 'vitest';

import {
  SKILL_TAG_PREFIX,
  isSkillTag,
  mergeTags,
  normalizeSkillEntry,
  normalizeTagEntry,
  partitionTags,
  skillName,
  toSkillTag,
} from './vaultTags';

describe('skill tag helpers', () => {
  it('detects and strips the reserved prefix', () => {
    expect(SKILL_TAG_PREFIX).toBe('skill:');
    expect(isSkillTag('skill:github-release')).toBe(true);
    expect(isSkillTag('deploy')).toBe(false);
    expect(skillName('skill:deploy-aws')).toBe('deploy-aws');
    expect(skillName('prod')).toBe('prod');
  });

  it('wraps a bare skill name once', () => {
    expect(toSkillTag('github-release')).toBe('skill:github-release');
    expect(toSkillTag('skill:github-release')).toBe('skill:github-release');
    expect(toSkillTag('  deploy  ')).toBe('skill:deploy');
    expect(toSkillTag('')).toBe('');
  });
});

describe('tag entry normalization', () => {
  it('trims and rejects empty or whitespace-bearing tags', () => {
    expect(normalizeTagEntry('  prod ')).toBe('prod');
    expect(normalizeTagEntry('')).toBeNull();
    expect(normalizeTagEntry('two words')).toBeNull();
  });

  it('rejects reserved skill: tags in the tag field (skills belong in the skills input)', () => {
    expect(normalizeTagEntry('skill:deploy')).toBeNull();
    // The skills input strips the prefix first, so it still accepts the same entry.
    expect(normalizeSkillEntry('skill:deploy')).toBe('deploy');
  });

  it('normalizes skill entries to a bare, prefix-free name', () => {
    expect(normalizeSkillEntry('github-release')).toBe('github-release');
    // A pasted `skill:` entry is accepted but not double-prefixed.
    expect(normalizeSkillEntry('skill:deploy')).toBe('deploy');
    expect(normalizeSkillEntry('bad name')).toBeNull();
  });
});

describe('partitionTags', () => {
  it('splits a flat tag list into normal tags and bare skills', () => {
    const { tags, skills } = partitionTags(['prod', 'skill:github-release', 'deploy', 'skill:deploy-aws']);
    expect(tags).toEqual(['prod', 'deploy']);
    expect(skills).toEqual(['github-release', 'deploy-aws']);
  });

  it('is resilient to nullish, blank, and non-string entries', () => {
    expect(partitionTags(null)).toEqual({ tags: [], skills: [] });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect(partitionTags(['  ', 'prod', 123 as any, 'prod'])).toEqual({ tags: ['prod'], skills: [] });
  });
});

describe('mergeTags', () => {
  it('re-prefixes skills and dedupes the merged list', () => {
    expect(mergeTags(['prod', 'deploy'], ['github-release'])).toEqual(['prod', 'deploy', 'skill:github-release']);
  });

  it('round-trips with partitionTags', () => {
    const flat = ['prod', 'skill:github-release', 'deploy'];
    const { tags, skills } = partitionTags(flat);
    expect(mergeTags(tags, skills)).toEqual(['prod', 'deploy', 'skill:github-release']);
  });

  it('drops blanks and collapses duplicates across both inputs', () => {
    expect(mergeTags(['prod', ' prod ', ''], ['deploy', 'skill:deploy'])).toEqual(['prod', 'skill:deploy']);
  });
});
