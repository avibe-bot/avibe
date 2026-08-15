import { describe, expect, it } from 'vitest';

import { runtimeCanAttemptInstall } from './runtimeLifecycle';
import type { RuntimeDependency } from './types';

const asset = (platform: 'darwin-arm64' | 'linux-amd64') => ({
  platform,
  url: `https://example.test/${platform}.tar.gz`,
  size_bytes: 1,
  sha256: 'a'.repeat(64),
});

const runtime = (
  assets: RuntimeDependency['manifest']['assets'],
): RuntimeDependency => ({
  contract_version: 5,
  host_platform: 'darwin-arm64',
  manifest: {
    name: 'cliproxyapi',
    version: 'v1',
    source_sha: 'b'.repeat(40),
    assets,
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
    expect(runtimeCanAttemptInstall(runtime([]))).toBe(true);
  });

  it('accepts an exact host asset and rejects a definitive mismatch', () => {
    expect(runtimeCanAttemptInstall(runtime([asset('darwin-arm64')]))).toBe(true);
    expect(runtimeCanAttemptInstall(runtime([asset('linux-amd64')]))).toBe(false);
  });
});
