import { describe, expect, it } from 'vitest';

import { runtimeCanAttemptInstall } from './runtimeLifecycle';
import type { RuntimeDependency } from './types';

const asset = (platform: 'darwin-arm64' | 'linux-amd64') => ({
  platform,
  url: `https://example.test/${platform}.tar.gz`,
  size_bytes: 1,
  sha256: 'a'.repeat(64),
});

const runtime = (resolution: 'resolved' | 'unresolved' | 'unsupported'): RuntimeDependency => ({
  contract_version: 10,
  host_platform: 'darwin-arm64',
  manifest: resolution === 'unresolved'
    ? { name: 'cliproxyapi', resolution, assets: [] }
    : {
      name: 'cliproxyapi',
      resolution,
      version: 'v1',
      source_sha: 'b'.repeat(40),
      assets: [asset(resolution === 'resolved' ? 'darwin-arm64' : 'linux-amd64')],
    },
  status: {
    installed_version: null,
    verified: false,
    listening: null,
    health: 'not_installed',
    last_check: null,
    error_key: null,
  },
});

describe('runtimeCanAttemptInstall', () => {
  it('keeps an uncached remote manifest eligible for server admission', () => {
    expect(runtimeCanAttemptInstall(runtime('unresolved'))).toBe(true);
  });

  it('accepts resolved admission and rejects explicit unsupported admission', () => {
    expect(runtimeCanAttemptInstall(runtime('resolved'))).toBe(true);
    expect(runtimeCanAttemptInstall(runtime('unsupported'))).toBe(false);
  });
});
