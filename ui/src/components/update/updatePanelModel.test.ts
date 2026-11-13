import { describe, expect, it } from 'vitest';
import type { VersionInfo } from '../../context/ApiContext';
import { modelFromDesktopRelay } from './updatePanelModel';

const quiet = { requesting: false, error: null, timedOut: false, disconnected: false };
const relay = (info: Partial<VersionInfo>): VersionInfo => ({
  current: '3.2.6', latest: null, has_update: false, error: null, managed_by: 'desktop',
  phase: 'current', desktop_version: '3.2.6', prereleases: false, checked_at: '2026-11-13T07:00:00Z', ...info,
});
const receipt = (phase: 'completed' | 'failed', target: string, updatedAt: string) => ({
  request_id: 'request-1', target, phase, progress: null, error_code: null, updated_at: updatedAt,
});

describe('a settled remote request on a desktop-managed host', () => {
  it('gives way to a newer release the host offers after installing the last one', () => {
    // The shell keeps the terminal receipt across later checks.
    const model = modelFromDesktopRelay(relay({
      phase: 'available', latest: '3.2.8', has_update: true, checked_at: '2026-11-13T06:00:00Z',
      remote_request: receipt('completed', '3.2.6', '2026-11-13T06:30:00Z'),
    }), quiet);
    expect(model.status).toEqual({ kind: 'available', version: '3.2.8' });
    expect(model.latest).toBe('3.2.8');
  });

  it('gives way to a check that finished after it', () => {
    const model = modelFromDesktopRelay(relay({
      phase: 'available', latest: '3.2.7', has_update: true, checked_at: '2026-11-13T08:00:00Z',
      remote_request: receipt('failed', '3.2.7', '2026-11-13T07:30:00Z'),
    }), quiet);
    expect(model.status).toEqual({ kind: 'available', version: '3.2.7' });
  });

  it('still reports its outcome until the host checks again', () => {
    const model = modelFromDesktopRelay(relay({
      phase: 'available', latest: '3.2.7', has_update: true, checked_at: '2026-11-13T07:00:00Z',
      remote_request: receipt('failed', '3.2.7', '2026-11-13T07:30:00Z'),
    }), quiet);
    expect(model.status).toEqual({ kind: 'update-failed', reason: 'failed', retry: true });
  });
});

describe('a setting change on its way to the host', () => {
  it('shows the value asked for until the host answers', () => {
    // The relay reports any live request as `checking`.
    const model = modelFromDesktopRelay(relay({
      phase: 'checking', auto_update: false,
      control_request: { request_id: 'control-1', action: 'auto_update', enabled: true, phase: 'checking', error_code: null, updated_at: '2026-11-13T07:05:00Z' },
    }), quiet);
    expect(model.autoUpdate).toEqual({ enabled: true });
    expect(model.status).toEqual({ kind: 'current' });
  });
});
