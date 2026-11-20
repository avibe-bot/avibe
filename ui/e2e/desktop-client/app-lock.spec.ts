import { test, expect, type Page } from '@playwright/test';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { createInterface } from 'node:readline';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { APP_LOCK_FIXTURES, type AppLockStatus, type AppLockSetResult } from '../../src/lib/appLockContract';

type Snapshot = {
  status: AppLockStatus; locked: boolean; verifying: boolean; authCalls: number;
  persistedPolicy: string | null; pending: boolean; executed: string[]; mainTitle: string; floatingVisible: boolean[]; quitAlertVisible: boolean;
  windows: { title: string; covered: boolean; hidden: boolean; contentCount: number; compact: boolean; firstResponderIsCover: boolean; button: string; pendingLabel: string; buttonTop: number; pendingBottom: number }[];
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
    this.process.on('exit', (code, signal) => {
      for (const pending of this.pending.values()) pending.reject(new Error(`Native peer exited ${code ?? signal}: ${diagnostics}`));
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
    if (this.process.exitCode === null && this.process.signalCode === null) {
      const exited = new Promise<void>((resolve) => this.process.once('exit', () => resolve()));
      const timeout = setTimeout(() => this.process.kill('SIGKILL'), 5000);
      this.process.stdin.end();
      await exited;
      clearTimeout(timeout);
    }
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


test('Mini stays hidden while locked and restores only the latest visibility intent', async () => {
  await peer.send('floating', { visible: true });
  const locked = await peer.send('reset', { policy: 'immediately' });
  expect(locked.floatingVisible).toEqual([false, false]);
  expect((await peer.send('unlock')).floatingVisible[0]).toBe(true);
  await peer.send('reset', { policy: 'immediately' });
  await peer.send('floating', { visible: false });
  expect((await peer.send('unlock')).floatingVisible[0]).toBe(false);
  await peer.send('reset', { policy: 'immediately' });
  expect((await peer.send('floating', { visible: true })).floatingVisible[0]).toBe(false);
  expect((await peer.send('unlock')).floatingVisible[0]).toBe(true);
  await peer.send('floating', { visible: false });
});

test('Quit confirmation is usable while every content window remains locked', async () => {
  await peer.send('reset', { policy: 'immediately' });
  const { during, after: cancelled } = await peer.send<{ during: Snapshot; after: Snapshot }>('quitAlert');
  expect(during.quitAlertVisible).toBe(true);
  expect(during.locked).toBe(true);
  expect(during.authCalls).toBe(0);
  expect(during.windows.every((window) => window.covered && window.hidden)).toBe(true);
  expect(cancelled.executed).toContain('quit:Cancel');
  expect(cancelled.locked).toBe(true);
});

test('the native main cover can be captured without exposing content', async ({}, testInfo) => {
  await peer.send('reset', { policy: 'immediately' });
  for (const dark of [false, true]) {
    await peer.send('appearance', { dark });
    const name = `native-lock-${dark ? 'dark' : 'light'}`;
    const path = testInfo.outputPath(`${name}.png`);
    await peer.send('capture', { path });
    await testInfo.attach(name, { path, contentType: 'image/png' });
  }
  await peer.send('appearance', { dark: false });
});


test('a verification reply before foreground restoration cannot relock Immediately', async () => {
  await peer.send('outcome', { value: null });
  const setting = peer.send<AppLockSetResult>('setPolicy', { policy: 'immediately' });
  await peer.send('leave', { time: 1 });
  const waiting = await peer.send('outcome', { value: 'success' });
  expect(waiting.verifying).toBe(true);
  await peer.send('arrive', { time: 2 });
  expect(await setting).toMatchObject({ ok: true });
  await peer.send('leave', { time: 3 });
  expect((await peer.send('unlock')).locked).toBe(true);
  const unlocked = await peer.send('arrive', { time: 4 });
  expect(unlocked.locked).toBe(false);
  expect(unlocked.verifying).toBe(false);
  expect(unlocked.authCalls).toBe(2);
});

test('pending copy describes the entry and window titles stay private until unlock', async () => {
  await peer.send('reset', { policy: 'immediately' });
  for (const [kind, text] of [['action', ''], ['navigation', 'link'], ['content', 'Vibey']] as const) {
    const state = await peer.send('entry', { kind, value: 'PRIVATE FIXTURE' });
    for (const window of state.windows) {
      if (!text) expect(window.pendingLabel).toBe('');
      else {
        expect(window.pendingLabel).toContain(text);
        expect(window.buttonTop).toBeLessThan(window.pendingBottom);
      }
    }
  }
  const changed = await peer.send('title', { value: 'PRIVATE NEW TITLE' });
  expect(changed.mainTitle).not.toContain('PRIVATE');
  expect(changed.windows.every((window) => !window.title.includes('PRIVATE'))).toBe(true);
  expect((await peer.send('unlock')).mainTitle).toBe('PRIVATE NEW TITLE');
});

test('real AppKit status item and open menu retain their native controls while locked', async () => {
  await peer.send('reset', { policy: 'immediately' });
  const result = await peer.send<{ trayVisible: boolean; chrome: { class: string; covered: boolean }[] }>('chrome');
  expect(result.trayVisible).toBe(true);
  expect(result.chrome.some((window) => /Menu|GlassEffect/.test(window.class))).toBe(true);
  expect(result.chrome.every((window) => !window.covered)).toBe(true);
});


test('expired success never persists a setting or unlocks on an hour-late arrival', async () => {
  await peer.send('setPolicy', { policy: 'oneMinute' });
  await peer.send('outcome', { value: null });
  const setting = peer.send<AppLockSetResult>('setPolicy', { policy: 'off' });
  await peer.send('leave', { time: 1 });
  await peer.send('tick', { time: 62 });
  await peer.send('outcome', { value: 'success' });
  const expired = await peer.send('tick', { time: 64 });
  expect(await setting).toMatchObject({ ok: false, reason: 'failed', status: { policy: 'oneMinute' } });
  expect(expired.persistedPolicy).toBe('oneMinute');
  expect(expired.locked).toBe(true);
  await peer.send('outcome', { value: null });
  const late = await peer.send('arrive', { time: 3600 });
  expect(late.locked).toBe(true);
  expect(late.verifying).toBe(false);
  await peer.send('unlock');
  await peer.send('leave', { time: 3601 });
  await peer.send('outcome', { value: 'success' });
  // No explicit tick: Arrive itself must reject the old success.
  const later = await peer.send('arrive', { time: 7200 });
  expect(later.locked).toBe(true);
  expect(later.verifying).toBe(false);
  expect(later.authCalls).toBe(3);
});

test('sleep invalidates a successful setting reply still waiting for activation', async () => {
  await peer.send('setPolicy', { policy: 'immediately' });
  await peer.send('outcome', { value: null });
  const setting = peer.send<AppLockSetResult>('setPolicy', { policy: 'off' });
  await peer.send('leave', { time: 1 });
  await peer.send('outcome', { value: 'success' });
  const sleeping = await peer.send('sleep', { time: 1.5 });
  expect(await setting).toMatchObject({ ok: false, reason: 'cancelled' });
  expect(sleeping.persistedPolicy).toBe('immediately');
  await peer.send('outcome', { value: null });
  expect((await peer.send('wake', { time: 10 })).locked).toBe(true);
});
