// Version helpers every update surface shares. The desktop app stamps SemVer
// (`3.1.2-rc.3`) and the Runtime reports PEP 440 (`3.1.2rc3`); surfaces show
// and compare the PEP 440 form so the same release reads the same everywhere.

const PEP440 = /^v?(\d+(?:\.\d+)*)(?:[-_.]?(a|b|c|rc|alpha|beta|pre|preview)[-_.]?(\d*))?(?:[-_.]?(post|rev|r)[-_.]?(\d*))?(?:[-_.]?(dev)[-_.]?(\d*))?(?:\+([a-z0-9]+(?:[-_.][a-z0-9]+)*))?$/;
const PRE_RELEASE: Record<string, string> = { alpha: 'a', beta: 'b', c: 'rc', pre: 'rc', preview: 'rc' };

export function normalizeVersion(value: string): string {
  const version = value.trim().toLowerCase();
  const match = PEP440.exec(version);
  if (!match) return version;
  const [, release, pre, preNumber, post, postNumber, dev, devNumber, local] = match;
  return [
    release,
    pre ? `${PRE_RELEASE[pre] ?? pre}${preNumber || '0'}` : '',
    post ? `.post${postNumber || '0'}` : '',
    dev ? `.dev${devNumber || '0'}` : '',
    local ? `+${local.replace(/[-_]/g, '.')}` : '',
  ].join('');
}

/** A version (in the PEP 440 form above) is a pre-release when it has a pre or dev segment. */
export function isPrereleaseVersion(version: string): boolean {
  const match = PEP440.exec(version);
  return Boolean(match && (match[2] || match[6]));
}

/** Whether `version`'s release (X.Y.Z, before any pre-release) precedes `other`'s. */
export function releasePrecedes(version: string, other: string): boolean {
  const release = (value: string) => (PEP440.exec(value)?.[1] ?? '').split('.').map(Number);
  const [a, b] = [release(version), release(other)];
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    const difference = (a[index] ?? 0) - (b[index] ?? 0);
    if (difference) return difference < 0;
  }
  return false;
}

/** Release candidates are published as GitHub-only `gh-vX.Y.ZrcN` tags (docs/release.md). */
export function releaseNotesUrl(version: string): string {
  const tag = isPrereleaseVersion(version) ? `gh-v${version}` : `v${version}`;
  return `https://github.com/avibe-bot/avibe/releases/tag/${encodeURIComponent(tag)}`;
}
