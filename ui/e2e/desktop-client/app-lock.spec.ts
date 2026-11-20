import { test, expect, type Page } from '@playwright/test';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { createInterface } from 'node:readline';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { APP_LOCK_FIXTURES, type AppLockStatus, type AppLockSetResult } from '../../src/lib/appLockContract';

type Snapshot = {
  status: AppLockStatus; locked: boolean; verifying: boolean; authCalls: number;
  pending: boolean; executed: string[];
  windows: { covered: boolean; hidden: boolean; contentCount: number; compact: boolean; firstResponderIsCover: boolean; button: string; pendingLabel: string }[];
};
class NativePeer {
  process: ChildProcessWithoutNullStreams;
  serial = 0;
  pending = new Map<number, { resolve: (value: unknown) => void; reject: (error: Error) => void }>();
  directory = mkdtempSync(join(tmpdir(), 'avibe-app-lock-e2e-'));
  ready: Promise<unknown>;
  constructor() {
    this.ready = new Promise((resolve, reject) => this.pending.set(0, { resolve, reject }));
    this.process = spawn(process.env.AVIBE_APP_LOCK_PEER!, [], {
      env: { ...process.env, HOME: this.directory, XDG_CONFIG_HOME: join(this.directory, 'config'), XDG_DATA_HOME: join(this.directory, 'data'), XDG_CACHE_HOME: join(this.directory, 'cache') },
      stdio: 'pipe',
    });
    let diagnostics = '';
    this.process.stderr.on('data', (data: Buffer) => { diagnostics += data.toString(); });
    createInterface({ input: this.process.stdout }).on('line', (line) => {
      const response = JSON.parse(line) as { id: number; value?: unknown; error?: string };
      const pending = this.pending.get(response.id);
      if (!pending) return;
      this.pending.delete(response.id);
      if (response.error) pending.reject(new Error(response.error)); else pending.resolve(response.value);
    });
    this.process.on('exit', (code) => {
      for (const pending of this.pending.values()) pending.reject(new Error(`Native peer exited ${code}: ${diagnostics}`));
      this.pending.clear();
    });
  }
  send<T = Snapshot>(op: string, payload: Record<string, unknown> = {}): Promise<T> {
    const id = ++this.serial;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve: (value) => resolve(value as T), reject });
      this.process.stdin.write(`${JSON.stringify({ id, op, ...payload })}\n`);
    });
  }
  async close() {
    const exited = new Promise<void>((resolve) => this.process.once('exit', () => resolve()));
    this.process.stdin.end();
    await exited;
    rmSync(this.directory, { recursive: true, force: true });
  }
}
let peer: NativePeer;
test.skip(process.platform !== 'darwin' || !process.env.AVIBE_APP_LOCK_PEER, 'Requires the explicitly compiled app-lock-e2e native peer.');
test.beforeAll(async () => { peer = new NativePeer(); await peer.ready; });
test.afterAll(async () => { if (peer) await peer.close(); });
test.beforeEach(async () => { await peer.send('reset'); });

async function settings(page: Page) {
  await page.exposeFunction('__nativeAppLock', (command: string, args: { policy?: string }) => {
    if (command === 'app_lock_status') return peer.send('status');
    if (command === 'app_lock_set_policy') return peer.send('setPolicy', { policy: args.policy });
    throw new Error(`Unadvertised native command: ${command}`);
  });
  await page.addInitScript(() => {
    localStorage.setItem('language', 'en');
    Object.defineProperty(window, '__AVIBE_DESKTOP_APP_LOCK__', { value: true });
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { value: true });
    Object.defineProperty(window, '__TAURI_INTERNALS__', { value: { invoke: (command: string, args = {}) => (window as unknown as { __nativeAppLock: (command: string, args: object) => Promise<unknown> }).__nativeAppLock(command, args) } });
  });
  await page.goto('/e2e/desktop-client/app-lock.html');
  return page.getByRole('combobox', { name: 'App Lock' });
}

test('the Rust owner decodes every payload from the frozen shared contract', async () => {
  for (const value of Object.values(APP_LOCK_FIXTURES)) {
    expect(await peer.send('decode', { value })).toEqual('status' in value ? value.status : value);
  }
  await expect(peer.send('setPolicy', { policy: 'unsupported' })).rejects.toThrow();
});

test('the real settings adapter waits for verification and preserves the old choice on cancel', async ({ page }) => {
  const select = await settings(page);
  await expect(select).toHaveValue('off');
  await peer.send('outcome', { value: null });
  await select.selectOption('oneMinute');
  await expect(select).toBeDisabled();
  await expect(page.getByText('Verifying…')).toBeVisible();
  await peer.send('outcome', { value: 'cancelled' });
  await expect(select).toBeEnabled();
  await expect(select).toHaveValue('off');
  await expect(page.getByRole('alert')).toHaveText('Not verified. The setting did not change.');
  await peer.send('outcome', { value: 'success' });
  await select.selectOption('oneMinute');
  await expect(select).toHaveValue('oneMinute');
  expect((await peer.send('snapshot')).authCalls).toBe(2);
});

test('settings without an account password explain why the stored policy cannot engage', async ({ page }) => {
  await peer.send('reset', { policy: 'fiveMinutes', availability: { available: false, biometry: 'none', biometryState: 'unavailable' } });
  const select = await settings(page);
  await expect(select).toHaveValue('off');
  await expect(select).toBeDisabled();
  await expect(page.getByText(/This Mac user has no password/)).toBeVisible();
  const snapshot = await peer.send('snapshot');
  expect(snapshot.locked).toBe(false);
  expect(snapshot.status.policy).toBe('fiveMinutes');
  expect(snapshot.authCalls).toBe(0);
});

test('the AppKit cover owns every content window and covers new native windows and views', async () => {
  await peer.send('setPolicy', { policy: 'immediately' });
  const locked = await peer.send('leave', { time: 1 });
  expect(locked.locked).toBe(true);
  expect(locked.windows.length).toBeGreaterThanOrEqual(3); // main, Show and an unregistered native Vault-shaped window
  expect(locked.windows.filter((window) => !window.compact)).toHaveLength(1);
  for (const window of locked.windows) {
    expect(window.covered && window.hidden && window.firstResponderIsCover).toBe(true);
    expect(window.contentCount).toBeGreaterThan(0);
  }
  const added = await peer.send('newWindow');
  expect(added.windows).toHaveLength(locked.windows.length + 1);
  const changed = await peer.send('newContent');
  for (const window of changed.windows) expect(window.covered && window.hidden).toBe(true);
  await peer.send('outcome', { value: 'cancelled' });
  const cancelled = await peer.send('arrive', { time: 2 });
  expect(cancelled.locked).toBe(true);
  expect(cancelled.authCalls).toBe(2); // the settings change and the single automatic unlock
  const arrivedAgain = await peer.send('arrive', { time: 3 });
  expect(arrivedAgain.authCalls).toBe(cancelled.authCalls);
  await peer.send('outcome', { value: 'success' });
  const unlocked = await peer.send('unlock'); // actual NSButton target/action
  expect(unlocked.locked).toBe(false);
  expect(unlocked.windows).toEqual([]);
});

test('the continuous-time threshold covers before return and includes sleep', async () => {
  await peer.send('setPolicy', { policy: 'oneMinute' });
  await peer.send('leave', { time: 0 });
  expect((await peer.send('tick', { time: 59 })).locked).toBe(false);
  await peer.send('arrive', { time: 59 });
  expect((await peer.send('tick', { time: 61 })).locked).toBe(false);
  await peer.send('sleep', { time: 100 });
  await peer.send('outcome', { value: null });
  const wake = await peer.send('wake', { time: 161 });
  expect(wake.locked).toBe(true);
  expect(wake.verifying).toBe(true);
  expect(wake.windows.every((window) => window.covered && window.hidden)).toBe(true);
});

test('closed-lid biometry keeps its Touch ID type but the native button offers the password', async () => {
  await peer.send('reset', { policy: 'fiveMinutes', availability: { available: true, biometry: 'touchID', biometryState: 'unavailable' } });
  const state = await peer.send('snapshot');
  expect(state.locked).toBe(true);
  for (const window of state.windows) expect(window.button).toMatch(/Password|登录密码/);
  expect((await peer.send('unlock')).locked).toBe(false);
});

test('one memory-only pending entry survives cancellation and only the latest executes', async () => {
  await peer.send('reset', { policy: 'immediately' });
  await peer.send('entry', { value: 'avibe://session/old-fixture' });
  const queued = await peer.send('entry', { value: 'ASK VIBEY PRIVATE SELECTION' });
  expect(queued.executed).toEqual([]);
  expect(queued.pending).toBe(true);
  for (const window of queued.windows) expect(window.pendingLabel).not.toContain('PRIVATE SELECTION');
  await peer.send('outcome', { value: 'cancelled' });
  expect((await peer.send('unlock')).executed).toEqual([]);
  await peer.send('outcome', { value: 'success' });
  const unlocked = await peer.send('unlock');
  expect(unlocked.executed).toEqual(['ASK VIBEY PRIVATE SELECTION']);
  expect(unlocked.pending).toBe(false);
});

test('authentication focus events never create a second immediate lock', async () => {
  await peer.send('outcome', { value: null });
  const changing = peer.send<AppLockSetResult>('setPolicy', { policy: 'immediately' });
  await peer.send('leave', { time: 1 });
  await peer.send('arrive', { time: 2 });
  const busy = await peer.send<AppLockSetResult>('setPolicy', { policy: 'fiveMinutes' });
  expect(busy).toMatchObject({ ok: false, reason: 'busy' });
  await peer.send('outcome', { value: 'success' });
  expect(await changing).toMatchObject({ ok: true, status: { policy: 'immediately' } });
  const state = await peer.send('snapshot');
  expect(state.locked).toBe(false);
  expect(state.authCalls).toBe(1);
});
