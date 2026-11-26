import type { DesktopRemoteUpdateRequest, UpdateSnapshot } from './types';

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

/** A standalone Runtime's unprompted version reads leave silent releases out (docs/release.md); the user's own
 *  check finds them. That check's offer stands over later reads while the Runtime runs the same version, until
 *  the next check answers. */
export function keepCheckedOffer<T extends UpdateSnapshot>(read: T, checked: UpdateSnapshot | null): T {
  if (!checked?.has_update || !checked.latest || read.managed_by
    || normalizeVersion(read.current) !== normalizeVersion(checked.current)
    || (read.has_update && read.latest === checked.latest)) {
    return read;
  }
  return { ...read, latest: checked.latest, has_update: true };
}

const TERMINAL_REMOTE_PHASES = ['completed', 'failed', 'declined', 'expired'];

/** A remote install request has ended: failed, declined, expired, or completed and confirmed by the running version. */
export function remoteRequestSettled(request: DesktopRemoteUpdateRequest | null | undefined, current: string): boolean {
  return Boolean(request && TERMINAL_REMOTE_PHASES.includes(request.phase)
    && (request.phase !== 'completed' || normalizeVersion(current) === normalizeVersion(request.target)));
}

/** An install or its restart is under way on this Avibe: a standalone Runtime reports it (`install`),
 *  a desktop-managed host relays its desktop app's request (`remote_request`). */
export function installRunning(info: UpdateSnapshot | null | undefined): boolean {
  if (!info) return false;
  if (info.install) return true;
  // Every phase before the request settles, the host's check and its wait for someone there included.
  return Boolean(info.remote_request && !remoteRequestSettled(info.remote_request, info.current));
}
