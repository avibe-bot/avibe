import { test, expect, type Page } from '@playwright/test';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { createInterface } from 'node:readline';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { APP_LOCK_FIXTURES, type AppLockStatus, type AppLockSetResult } from '../../src/lib/appLockContract';

type Snapshot = {
  awaySince: number | null; candidateLeave: number | null; blocked: number; shouldPrompt: boolean;
  status: AppLockStatus; locked: boolean; verifying: boolean; authCalls: number;
  persistedPolicy: string | null; pending: boolean; executed: string[]; mainTitle: string; floatingVisible: boolean[]; quitAlertVisible: boolean;
  windows: { label: string | null; title: string; covered: boolean; hidden: boolean; contentCount: number; compact: boolean; firstResponderIsCover: boolean; button: string; pendingLabel: string; buttonTop: number; pendingBottom: number }[];
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

test('E36 default Off leaves native windows and entry actions unlocked until opt-in', async () => {
  for (const [event, time] of [['leave', 1], ['tick', 3600], ['arrive', 3601]] as const) {
    const state = await peer.send(event, { time });
    expect(state.status.policy).toBe('off');
    expect(state.locked).toBe(false);
    expect(state.authCalls).toBe(0);
    expect(state.windows).toEqual([]);
  }
  expect((await peer.send('newWindow')).windows).toEqual([]);
  const expected: string[] = [];
  for (const kind of ['action', 'command', 'content', 'navigation']) {
    expected.push(kind);
    const state = await peer.send('entry', { kind, value: kind });
    expect(state.executed).toEqual(expected);
    expect(state.pending).toBe(false);
    expect(state.authCalls).toBe(0);
  }
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
  expect(wake.verifying).toBe(false);
  expect((await peer.send('arrive', { time: 162 })).verifying).toBe(true);
  expect(wake.windows.every((window) => window.covered && window.hidden)).toBe(true);
});

test('closed-lid biometry keeps its Touch ID type but the native button offers the password', async () => {
  await peer.send('reset', { policy: 'fiveMinutes', availability: { available: true, biometry: 'touchID', biometryState: 'unavailable' } });
  const state = await peer.send('snapshot');
  expect(state.locked).toBe(true);
  for (const window of state.windows) expect(window.button).toMatch(/Password|登录密码/);
  expect((await peer.send('unlock')).locked).toBe(false);
});

test('W1 physical App/Show windows inherit covers before and after locking, including followed document titles', async () => {
  await peer.send('appWindow', { label: 'app-editor-lock-fixture' });
  await expect.poll(() => peer.send('appWindowTitles')).toEqual(['PRIVATE DOCUMENT TITLE', null]);
  await peer.send('reset', { policy: 'immediately' });
  const before = await peer.send('snapshot');
  const added = await peer.send('appWindow', { label: 'app-show-lock-fixture' });
  expect(added.windows.length).toBeGreaterThan(before.windows.length);
  for (const window of added.windows) {
    expect(window).toMatchObject({ covered: true, hidden: true });
    expect(window.title).not.toContain('PRIVATE');
  }
  // Wait for WKWebView's title callback while locked, then consume the stored
  // title on unlock. The actual follower must not reveal the document title.
  await expect.poll(() => peer.send('appWindowLoads')).toEqual(['app-editor-lock-fixture', 'app-show-lock-fixture']);
  expect(await peer.send<string[]>('appWindowTitles')).toEqual(['Avibe is locked', 'Avibe is locked']);
  // The cover exists before WKWebView attaches its content; once both pages
  // load, assert the two real content trees are present and hidden as well.
  const loaded = (await peer.send('snapshot')).windows.filter((window) => window.label?.startsWith('app-'));
  expect(loaded).toHaveLength(2);
  for (const window of loaded) {
    expect(window).toMatchObject({ covered: true, hidden: true });
    expect(window.contentCount, JSON.stringify(window)).toBeGreaterThan(0);
  }
  await peer.send('unlock');
  await expect.poll(() => peer.send('appWindowTitles')).toEqual(['PRIVATE DOCUMENT TITLE', 'PRIVATE DOCUMENT TITLE']);
  await peer.send('closeAppWindows');
});

test('all four native Show buttons wait for unlock before page events or owner actions', async () => {
  // Workbench must be a real WebView so the lock gate can foreground it.
  await peer.send('composePage');
  await expect.poll(() => peer.send('composeRead')).not.toBeNull();
  await peer.send('appWindow', { label: 'app-show-lock-fixture' });
  await expect.poll(() => peer.send('appWindowLoads')).toEqual(['app-show-lock-fixture']);
  const events: string[] = [];
  for (const [tag, action] of [[0, 'Annotate'], [1, 'Chat'], [2, 'Share'], [3, 'Browser']] as const) {
    await peer.send('reset', { policy: 'immediately' });
    await peer.send('outcome', { value: 'cancelled' });
    const pressed = await peer.send('showPress', { tag });
    expect(pressed.locked).toBe(true);
    expect(pressed.executed).toEqual([]);
    expect(await peer.send('showRead')).toEqual(events);
    expect(await peer.send('appWindowTitles')).toEqual([null, 'Avibe is locked']);
    expect(pressed.pending).toBe(true);
    // The lock entry foregrounds main; replay must restore the page that owns
    // an in-window action rather than leave its popover behind Workbench.
    await expect.poll(() => peer.send('showPresentation')).toMatchObject({ focused: false });
    await peer.send('outcome', { value: 'success' });
    const unlocked = await peer.send('unlock');
    expect(unlocked).toMatchObject({ locked: false, pending: false });
    if (action === 'Annotate' || action === 'Share') {
      events.push(action === 'Annotate' ? 'avibe:show-annotate-open' : 'avibe:show-share-open');
      expect(unlocked.executed).toEqual([]);
      await expect.poll(() => peer.send('showPresentation')).toMatchObject({
        focused: true, visible: true, minimized: false,
      });
    } else {
      expect(unlocked.executed).toEqual([`show:${action}`]);
    }
    await expect.poll(() => peer.send('showRead')).toEqual(events);
    expect((await peer.send('tick', { time: 3 })).executed).toEqual(unlocked.executed);
    expect(await peer.send('showRead')).toEqual(events);
  }
  // Cancelled verification retains one slot: a later explicit command replaces
  // the earlier Browser request, so only Chat reaches the native owner.
  await peer.send('reset', { policy: 'immediately' });
  await peer.send('outcome', { value: 'cancelled' });
  await peer.send('showPress', { tag: 3 });
  await peer.send('showPress', { tag: 1 });
  expect((await peer.send('snapshot')).executed).toEqual([]);
  await peer.send('outcome', { value: 'success' });
  expect((await peer.send('unlock')).executed).toEqual(['show:Chat']);
  await peer.send('reset', { policy: 'immediately' });
  await peer.send('outcome', { value: 'cancelled' });
  await peer.send('showPress', { tag: 3 });
  await peer.send('showRoute');
  await peer.send('outcome', { value: 'success' });
  expect((await peer.send('unlock')).executed).toEqual([]);
  await peer.send('closeAppWindows');
});

test('W1 unsaved close/quit guard stays answerable while locked without revealing the page confirmation', async () => {
  await peer.send('appWindow', { label: 'app-editor-lock-fixture' });
  await expect.poll(() => peer.send('appWindowLoads')).toEqual(['app-editor-lock-fixture']);
  // Run the minimized case last: deminiaturize is animated by AppKit, so an
  // immediate hide would race that animation rather than test sheet ownership.
  for (const [policy, presentation, response, confirmed] of [
    ['immediately', 'hidden', 1001, false],
    ['oneMinute', 'hidden', 1001, false],
    ['immediately', 'minimized', 1000, true],
  ] as const) {
    await peer.send('reset', { policy });
    const locked = policy === 'immediately';
    if (!locked) await peer.send('unlock');
    await peer.send('mainPresentation', { value: presentation });
    const mainState = { visible: false, minimized: presentation === 'minimized' };
    await expect.poll(() => peer.send('mainPresentation')).toEqual(mainState);
    const answer = peer.send<{ confirmed: boolean; after: Snapshot }>('closeGuard');
    await expect.poll(() => peer.send('sheetRead')).toEqual({
      parent: 'app-editor-lock-fixture',
      title: 'Avibe', message: 'Discard unsaved changes and close this window?',
      buttons: ['Discard and Close', 'Cancel'],
    });
    expect(await peer.send('mainPresentation')).toEqual(mainState);
    const waiting = await peer.send('snapshot');
    expect(waiting.quitAlertVisible).toBe(true);
    expect(waiting.locked).toBe(locked);
    if (locked) expect(waiting.windows.every((window) => window.hidden && window.covered)).toBe(true);
    await peer.send('sheetAnswer', { response });
    const result = await answer;
    expect(result.confirmed).toBe(confirmed);
    expect(result.after.locked).toBe(locked);
    expect(result.after.authCalls).toBe(locked ? 0 : 1);
    expect(result.after.quitAlertVisible).toBe(false);
    // Cancelling an unlocked internal sheet cannot create an absence deadline.
    if (!locked) expect((await peer.send('tick', { time: 1000 })).locked).toBe(false);
  }
  await peer.send('mainPresentation', { value: 'shown' });
  await peer.send('closeAppWindows');
});

test('one memory-only pending entry survives cancellation and only the latest executes', async () => {
  await peer.send('reset', { policy: 'immediately' });
  await peer.send('entry', { kind: 'navigation', value: 'avibe://session/old-fixture' });
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

test('E28 foreground-only Dock and tray requests preserve pending navigation', async () => {
  await peer.send('reset', { policy: 'immediately' });
  await peer.send('outcome', { value: null });
  await peer.send('entry', { kind: 'navigation', value: 'old navigation' });
  await peer.send('entry', { kind: 'navigation', value: 'new navigation' });
  for (const value of ['dock', 'tray']) {
    const waiting = await peer.send('source', { value });
    expect(waiting.locked).toBe(true);
    expect(waiting.executed).toEqual([]);
    expect(waiting.windows.every((window) => window.pendingLabel.includes('link'))).toBe(true);
  }
  for (const args of [[], ['--foreground'], ['avibe://unsupported/path']]) {
    const waiting = await peer.send('source', { value: 'secondLaunch', arguments: args });
    expect(waiting.pending).toBe(true);
    expect(waiting.windows.every((window) => window.pendingLabel.includes('link'))).toBe(true);
  }
  const unlocked = await peer.send('outcome', { value: 'success' });
  expect(unlocked.executed).toEqual(['new navigation']);
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

test('a deferred connection menu click keeps the actual choice checked even when superseded', async () => {
  await peer.send('reset', { policy: 'immediately' });
  await peer.send('outcome', { value: 'cancelled' });
  const saved = { saved: 'automatic', automatic: true, tunnel: false };
  expect(await peer.send('directMenu')).toEqual(saved);
  for (const click of ['direct-connection-tunnel', 'direct-connection-automatic']) {
    await peer.send('directMenu', { click });
    await expect.poll(() => peer.send('directMenu')).toEqual(saved);
    expect(await peer.send('snapshot')).toMatchObject({ locked: true, pending: true });
  }
  await peer.send('entry', { kind: 'command', value: 'replacement command' });
  await peer.send('outcome', { value: 'success' });
  const unlocked = await peer.send('unlock');
  expect(unlocked.executed).toEqual(['replacement command']);
  expect(unlocked.pending).toBe(false);
  expect(await peer.send('directMenu')).toEqual(saved);
});

test('releases arriving while locked drain voice and Mini latches without admitting an action', async () => {
  for (const kind of ['voiceHold', 'voiceToggle', 'mini']) {
    for (const cancelled of [false, true]) {
      await peer.send('reset');
      const first = kind === 'mini' ? 'Toggle' : 'Voice(Listen)';
      expect(await peer.send('shortcutCycle', { kind, step: 'prepare' })).toBe(first);
      await peer.send('reset', { policy: 'immediately' });
      if (cancelled) await peer.send('shortcutCycle', { kind, step: 'cancelVoice' });
      await peer.send('shortcutCycle', { kind, step: 'release' });
      expect(await peer.send('snapshot')).toMatchObject({
        locked: true, pending: false, authCalls: 0, executed: [], floatingVisible: [false, false],
      });
      await peer.send('reset');
      const next = first; // Lock entry cancels the active take in either voice mode.
      expect(await peer.send('shortcutCycle', { kind, step: 'next' })).toBe(next);
      expect(await peer.send('shortcutCycle', { kind, step: 'next' })).toBeNull();
    }
  }
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

test('the native main cover can be captured without exposing content', async () => {
  const testInfo = test.info();
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
  expect(late.verifying).toBe(true); // a new visit starts a fresh evaluation
  expect(late.authCalls).toBe(3);
  await peer.send('leave', { time: 3601 });
  await peer.send('outcome', { value: 'success' });
  // No explicit tick: Arrive itself must reject the old success.
  await peer.send('tick', { time: 7200 });
  await peer.send('outcome', { value: null });
  const later = await peer.send('arrive', { time: 7200 });
  expect(later.locked).toBe(true);
  expect(later.verifying).toBe(true);
  expect(later.authCalls).toBe(4);
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


for (const policy of ['immediately', 'oneMinute', 'fiveMinutes'] as const) {
  test(`E20/E21 unlocked Quit and busy cancellation never create absence (${policy})`, async () => {
    await peer.send('setPolicy', { policy });
    for (const busy of [false, true]) {
      const { during, after } = await peer.send<{ during: Snapshot; after: Snapshot }>('quitAlert', { busy });
      expect(during.quitAlertVisible).toBe(true);
      expect(during.locked).toBe(false);
      expect(after.awaySince).toBeNull();
      expect((await peer.send('tick', { time: 1000 })).locked).toBe(false);
    }
    await peer.send('leave', { time: 1001 });
    await peer.send('quitAlert');
    const overdue = await peer.send('tick', { time: 1400 });
    expect(overdue.awaySince).toBe(1001);
    expect(overdue.locked).toBe(true);
  });
}

test('E19/N03 lifecycle cancellation preserves the LA candidate through the original return window', async () => {
  for (const returns of [false, true]) {
    await peer.send('reset');
    await peer.send('setPolicy', { policy: 'oneMinute' });
    await peer.send('outcome', { value: null });
    const setting = peer.send<AppLockSetResult>('setPolicy', { policy: 'off' });
    await peer.send('leave', { time: 1 });
    await peer.send('tick', { time: 10 });
    await peer.send('quitAlert');
    expect(await setting).toMatchObject({ ok: false, reason: 'cancelled' });
    if (returns) await peer.send('arrive', { time: 11 });
    await peer.send('outcome', { value: 'success' }); // old epoch cannot write
    const state = await peer.send('tick', { time: 1000 });
    expect(state.locked).toBe(!returns);
    expect(state.awaySince).toBe(returns ? null : 1);
    expect(state.persistedPolicy).toBe('oneMinute');
  }
});

test('N01/E40 another regular app cancels Change; LA and own helper activation do not', async () => {
  await peer.send('setPolicy', { policy: 'oneMinute' });
  await peer.send('outcome', { value: null });
  const setting = peer.send<AppLockSetResult>('setPolicy', { policy: 'off' });
  await peer.send('leave', { time: 1 });
  for (const [bundle, regular] of [['com.apple.coreautha', false], ['com.apple.SecurityAgent', false], ['io.avibe.test.helper', true]] as const) {
    const state = await peer.send('application', { bundle, regular });
    expect(state.verifying).toBe(true);
    expect(state.awaySince).toBeNull();
  }
  const departed = await peer.send('application', { bundle: 'com.apple.finder', regular: true });
  expect(await setting).toMatchObject({ ok: false, reason: 'cancelled' });
  expect(departed.awaySince).toBe(1);
  await peer.send('outcome', { value: 'success' });
  const later = await peer.send('tick', { time: 61 });
  expect(later.locked).toBe(true);
  expect(later.persistedPolicy).toBe('oneMinute');
});

test('N02/E25/E39 public activation recovers all notification hints; manual LA works if facts remain inconclusive', async () => {
  for (const [leave, back] of [['sleep', 'wake'], ['displaySleep', 'displayWake'], ['sessionLeave', 'sessionReturn'], ['screenLock', 'screenUnlock']]) {
    await peer.send('reset', { policy: 'immediately' });
    await peer.send('unlock');
    await peer.send('outcome', { value: null });
    await peer.send(leave, { time: 1 });
    const hinted = await peer.send(back, { time: 10 });
    expect(hinted.blocked).not.toBe(0);
    expect(hinted.awaySince).toBe(1);
    await peer.send('recovery', { valid: false });
    const inconclusive = await peer.send('arrive', { time: 11 });
    expect(inconclusive.locked).toBe(true);
    expect(inconclusive.authCalls).toBe(1);
    expect(inconclusive.blocked).not.toBe(0);
    expect((await peer.send('unlock')).authCalls).toBe(2);
    const recovered = await peer.send('outcome', { value: 'success' });
    expect(recovered.locked).toBe(false);
    expect(recovered.blocked).toBe(0);
    await peer.send(leave, { time: 12 }); // omit the paired return hint entirely
    await peer.send('recovery', { valid: true });
    await peer.send('outcome', { value: null });
    const fresh = await peer.send('arrive', { time: 13 });
    expect(fresh.blocked).toBe(0);
    expect(fresh.verifying).toBe(true);
    expect(fresh.authCalls).toBe(3);
  }
});

test('N04/E42 transient capability failures preserve native covers; passcode restoration starts locked', async () => {
  await peer.send('reset', { policy: 'immediately' });
  for (const error of [-4, -1004, -8]) {
    const state = await peer.send('capability', { error });
    expect(state.status.available).toBe(true);
    expect(state.locked).toBe(true);
    expect(state.windows.every((window) => window.hidden && window.covered)).toBe(true);
  }
  const missing = await peer.send('capability', { error: -5 });
  expect(missing.locked).toBe(false);
  expect(missing.status.available).toBe(false);
  expect((await peer.send('capability', { error: -4 })).status.available).toBe(false);
  const restored = await peer.send('capability', { error: null });
  expect(restored.locked).toBe(true);
  expect(restored.status.policy).toBe('immediately');
  expect((await peer.send('unlock')).locked).toBe(false);
});


test('E28/E29/E32/E33 native Dock, tray, menu, deep-link and Mini callers preserve the original departure', async () => {
  await peer.send('setPolicy', { policy: 'immediately' });
  await peer.send('leave', { time: 10 });
  await peer.send('outcome', { value: null });
  // Release and document completion alone cannot publish an entry or start LA.
  for (const value of ['shortcutRelease', 'documentFinished']) {
    const state = await peer.send('source', { value });
    expect(state.pending).toBe(false);
    expect(state.authCalls).toBe(1);
    expect(state.awaySince).toBe(10);
  }
  for (const value of ['dock', 'tray', 'settings', 'signOut', 'instances', 'link', 'shortcutPress']) {
    const state = await peer.send('source', { value });
    expect(state.pending).toBe(true);
    expect(state.locked).toBe(true);
    expect(state.awaySince).toBe(10);
    expect(state.executed).toEqual([]);
    expect(state.floatingVisible[0]).toBe(false);
  }
  const release = await peer.send('source', { value: 'shortcutRelease' });
  expect(release.pending).toBe(true);
  expect(release.executed).toEqual([]);
});

test('E22 Quit Keep and Stop choices do not request an unlock', async () => {
  for (const [response, answer] of [[1000, 'Yes'], [1001, 'No']] as const) {
    await peer.send('reset', { policy: 'immediately' });
    const { after } = await peer.send<{ after: Snapshot }>('quitAlert', { response });
    expect(after.executed).toContain(`quit:${answer}`);
    expect(after.authCalls).toBe(0);
    expect(after.locked).toBe(true);
  }
});

test('E30 Ask Vibey defers its real queue and file reads, then delivers through WKWebView to the shipped composer receiver', async () => {
  await peer.send('composePage');
  type Observed = { lines: string[]; files: { name: string; text: string }[]; notices: string[]; mounted: boolean; route: string };
  await expect.poll(() => peer.send<Observed | null>('composeRead')).toMatchObject({ lines: [], files: [], mounted: true });
  const baseline = await peer.send<{ admitted: number }>('composeQueue');
  await peer.send('reset', { policy: 'immediately' });
  await peer.send('outcome', { value: null });
  await peer.send('ask', { value: 'discarded older selection' });
  const pending = await peer.send('ask', { value: 'The selected contract clause.' });
  expect(pending.locked).toBe(true);
  expect(pending.pending).toBe(true);
  expect(pending.windows.every((window) => !window.pendingLabel.includes('contract clause'))).toBe(true);
  expect(await peer.send('composeQueue')).toEqual({ admitted: baseline.admitted, queued: 0 });
  expect((await peer.send<Observed>('composeRead')).lines).toEqual([]);
  await peer.send('outcome', { value: 'cancelled' });
  // A foreground-only Dock click after cancellation must not discard the
  // selected content or replace its generic pending copy.
  const dock = await peer.send('source', { value: 'dock' });
  expect(dock.locked).toBe(true);
  expect(dock.pending).toBe(true);
  expect(dock.windows.every((window) => window.pendingLabel.includes('Vibey'))).toBe(true);
  expect(dock.authCalls).toBe(pending.authCalls);
  expect((await peer.send('source', { value: 'tray' })).windows.every((window) => window.pendingLabel.includes('Vibey'))).toBe(true);
  for (const args of [[], ['--foreground'], ['avibe://unsupported/path']]) {
    expect((await peer.send('source', { value: 'secondLaunch', arguments: args })).windows.every((window) => window.pendingLabel.includes('Vibey'))).toBe(true);
  }
  await peer.send('unlock');
  expect(await peer.send('composeQueue')).toEqual({ admitted: baseline.admitted, queued: 0 });
  await peer.send('outcome', { value: 'success' });
  await peer.send('unlock');
  await expect.poll(async () => (await peer.send<Observed>('composeRead')).lines).toEqual(['"The selected contract clause."']);
  await expect.poll(() => peer.send('composeQueue')).toEqual({ admitted: baseline.admitted + 1, queued: 0 });

  await peer.send('reset', { policy: 'immediately' });
  await peer.send('outcome', { value: null });
  await peer.send('askFile', { value: null });
  await peer.send('ask', { kind: 'file' }); // deliberately absent when selected
  expect(await peer.send('composeQueue')).toEqual({ admitted: baseline.admitted + 1, queued: 0 });
  expect((await peer.send<Observed>('composeRead')).files).toEqual([]);
  // If enqueue/content opened the file while locked, it would be permanently
  // Refused(unreadable). Creating it now proves validation waits for unlock.
  await peer.send('askFile', { value: 'File becomes available while locked.' });
  expect((await peer.send('outcome', { value: 'success' })).locked).toBe(false);
  await expect.poll(async () => (await peer.send<Observed>('composeRead')).files).toEqual([
    { name: 'ask-fixture.txt', text: 'File becomes available while locked.' },
  ]);
  await expect.poll(() => peer.send('composeQueue')).toEqual({ admitted: baseline.admitted + 2, queued: 0 });
  const observed = await peer.send<Observed>('composeRead');
  expect(observed.notices).toEqual([]);
  expect(observed.lines).toEqual(['"The selected contract clause."']);
  expect(observed.route).toBe('/chat/ses_main');
  // With App Lock Off the same consumer still inserts without any LA call.
  await peer.send('reset');
  await peer.send('ask', { kind: 'url', value: 'https://example.com/contract' });
  await expect.poll(async () => (await peer.send<Observed>('composeRead')).lines).toEqual([
    '"The selected contract clause."', 'https://example.com/contract',
  ]);
  expect((await peer.send('snapshot')).authCalls).toBe(0);
  await expect.poll(() => peer.send('composeQueue')).toEqual({ admitted: baseline.admitted + 3, queued: 0 });

  // Also exercise an already admitted share whose receiver was not ready when
  // the user left. The production pump must wait while the window is locked.
  await peer.send('reset', { policy: 'immediately' });
  await peer.send('unlock');
  await peer.send('composeRead', { paused: true });
  await peer.send('ask', { value: 'Queued before departure.' });
  expect(await peer.send('composeQueue')).toEqual({ admitted: baseline.admitted + 4, queued: 1 });
  await peer.send('leave', { time: 10 });
  await peer.send('composeRead', { paused: false });
  await new Promise((resolve) => setTimeout(resolve, 2200)); // one real pump retry
  expect((await peer.send<Observed>('composeRead')).lines).toHaveLength(2);
  expect(await peer.send('composeQueue')).toEqual({ admitted: baseline.admitted + 4, queued: 1 });
  // A manual attempt while inactive returns through the same bounded window.
  await peer.send('unlock');
  await peer.send('arrive', { time: 11 });
  await expect.poll(async () => (await peer.send<Observed>('composeRead')).lines).toEqual([
    '"The selected contract clause."', 'https://example.com/contract', '"Queued before departure."',
  ]);
  await expect.poll(() => peer.send('composeQueue')).toEqual({ admitted: baseline.admitted + 4, queued: 0 });
});

test('native notification choice and Reply wait for unlock and preserve the original operation ID', async () => {
  await peer.send('composePage');
  type Answers = { answers: { path: string; body: Record<string, unknown> }[] };
  await expect.poll(() => peer.send<Answers | null>('composeRead')).toMatchObject({ answers: [] });
  const expected: Answers['answers'] = [];
  const revision = 'a'.repeat(64);
  for (const [kind, action, text] of [
    ['choice', 'avibe.choice.0', '✅ Merge private fixture'],
    ['text', 'avibe.reply', 'Private typed answer'],
  ] as const) {
    await peer.send('reset', { policy: 'immediately' });
    await peer.send('outcome', { value: 'cancelled' });
    const admitted = await peer.send<{ operationId: string; after: Snapshot }>('notificationAnswer', {
      question: { sessionId: 'ses_main', messageId: 'msg_fixture', revision, options: ['✅ Merge private fixture'], source: 'Fixture' },
      action, text,
    });
    expect(admitted.operationId).toMatch(/^desktop-answer-/);
    expect(admitted.after.locked).toBe(true);
    expect(admitted.after.pending).toBe(true);
    expect(admitted.after.windows.every((window) => window.pendingLabel === '')).toBe(true);
    expect((await peer.send<Answers>('composeRead')).answers).toEqual(expected);
    await peer.send('source', { value: 'dock' });
    await peer.send('source', { value: 'secondLaunch', arguments: [] });
    await peer.send('tick', { time: 3600 });
    expect(await peer.send('notificationNotices')).toEqual([]);
    expect((await peer.send<Answers>('composeRead')).answers).toEqual(expected);
    await peer.send('outcome', { value: 'success' });
    await peer.send('unlock');
    expected.push({ path: '/api/sessions/ses_main/messages', body: {
      text, operation_id: admitted.operationId, revision,
      ...(kind === 'choice' ? { metadata: { quick_reply_for: 'msg_fixture' } } : { in_reply_to: 'msg_fixture' }),
    } });
    await expect.poll(async () => (await peer.send<Answers>('composeRead')).answers).toEqual(expected);
    expect(await peer.send('notificationNotices')).toEqual([]);
    await peer.send('source', { value: 'dock' });
    expect((await peer.send<Answers>('composeRead')).answers).toEqual(expected);
  }
  await peer.send('reset');
  const off = await peer.send<{ operationId: string; after: Snapshot }>('notificationAnswer', {
    question: { sessionId: 'ses_main', messageId: 'msg_fixture', revision, options: ['Off answer'], source: 'Fixture' },
    action: 'avibe.choice.0',
  });
  expected.push({ path: '/api/sessions/ses_main/messages', body: {
    text: 'Off answer', operation_id: off.operationId, revision, metadata: { quick_reply_for: 'msg_fixture' },
  } });
  await expect.poll(async () => (await peer.send<Answers>('composeRead')).answers).toEqual(expected);
  expect(off.after.authCalls).toBe(0);
});

test('E46 replacing a pending notification answer reports not delivered and retains its Session link', async () => {
  await peer.send('composePage');
  type Answers = { answers: { body: { operation_id: string } }[] };
  await expect.poll(() => peer.send<Answers | null>('composeRead')).not.toBeNull();
  const baseline = (await peer.send<Answers>('composeRead')).answers;
  for (const [kind, action] of [
    ['command', 'avibe.choice.0'], ['content', 'avibe.reply'], ['navigation', 'avibe.reply'],
  ] as const) {
    await peer.send('reset', { policy: 'immediately' });
    await peer.send('outcome', { value: 'cancelled' });
    const original = await peer.send<{ operationId: string }>('notificationAnswer', {
      question: { sessionId: 'ses_main', messageId: 'msg_fixture', revision: 'a'.repeat(64), options: ['PRIVATE choice'], source: 'PRIVATE source' },
      action, text: 'PRIVATE reply', opens: 'avibe://session/ses_main',
    });
    await peer.send('source', { value: 'dock' });
    expect(await peer.send('notificationNotices')).toEqual([]);
    await peer.send('entry', { kind, value: 'replacement' });
    const notices = [{
      title: 'Not delivered', body: 'Open Avibe to answer again.', opens: 'avibe://session/ses_main',
    }];
    await expect.poll(() => peer.send('notificationNotices')).toEqual(notices);
    expect((await peer.send<Answers>('composeRead')).answers).toEqual(baseline);
    await peer.send('source', { value: 'dock' });
    await peer.send('outcome', { value: 'success' });
    expect((await peer.send('unlock')).executed).toEqual(['replacement']);
    expect(await peer.send('notificationNotices')).toEqual(notices);
    expect((await peer.send<Answers>('composeRead')).answers.some(
      (answer) => answer.body.operation_id === original.operationId,
    )).toBe(false);
  }
});

test('native lock publication invalidates managed document voice once and Off stays admitted', async () => {
  await peer.send('reset'); await peer.send('composePage');
  await expect.poll(() => peer.send('composeRead')).toMatchObject({ voiceAllowed: true });
  const before = await peer.send<{ voiceCancelled: number }>('composeRead');
  await peer.send('tick', { time: 3600 });
  expect(await peer.send('composeRead')).toMatchObject({ voiceAllowed: true, voiceCancelled: before.voiceCancelled });
  await peer.send('reset', { policy: 'immediately' });
  await expect.poll(() => peer.send('composeRead')).toMatchObject({ voiceAllowed: false, voiceCancelled: before.voiceCancelled + 1 });
  await peer.send('tick', { time: 3601 });
  expect(await peer.send('composeRead')).toMatchObject({ voiceAllowed: false, voiceCancelled: before.voiceCancelled + 1 });
  await peer.send('composePage', { reload: true });
  await expect.poll(() => peer.send('composeRead')).toMatchObject({ voiceAllowed: false });
  await peer.send('outcome', { value: 'success' }); await peer.send('unlock');
  await expect.poll(() => peer.send('composeRead')).toMatchObject({ voiceAllowed: true });
});

test('locked voice press is Action and repeats across unlock cannot start a take', async () => {
  for (const kind of ['voiceHold', 'voiceToggle']) {
    await peer.send('reset');
    await peer.send('shortcutCycle', { kind, step: 'prepare' });
    await peer.send('reset', { policy: 'immediately' });
    await peer.send('outcome', { value: 'cancelled' });
    await peer.send('entry', { kind: 'content', value: 'keep content' });
    await peer.send('shortcutCycle', { kind, step: 'nativePress' });
    expect(await peer.send('shortcutCycle', { kind, step: 'suppressed' })).toBe(true);
    await peer.send('outcome', { value: 'success' });
    const unlocked = await peer.send('unlock');
    expect(unlocked.executed).toEqual(['keep content']);
    await peer.send('shortcutCycle', { kind, step: 'nativePress' });
    expect(await peer.send('shortcutCycle', { kind, step: 'suppressed' })).toBe(true);
    await peer.send('shortcutCycle', { kind, step: 'release' });
    expect(await peer.send('shortcutCycle', { kind, step: 'suppressed' })).toBe(false);
    expect(await peer.send('shortcutCycle', { kind, step: 'next' })).toBe('Voice(Listen)');
    await peer.send('shortcutCycle', { kind, step: 'release' });
    await peer.send('shortcutCycle', { kind, step: 'stalePress' });
    expect(await peer.send('shortcutCycle', { kind, step: 'suppressed' })).toBe(true);
    await peer.send('shortcutCycle', { kind, step: 'release' });
    expect(await peer.send('shortcutCycle', { kind, step: 'suppressed' })).toBe(false);
  }
});


test('startup consumes explicit links before setup resume, Off and after unlock', async () => {
  const cases = [
    { arguments: ['avibe://session/explicit'], resume: true, target: '/chat/explicit', update: false },
    { arguments: ['avibe://show/explicit'], resume: true, target: '/apps/show/explicit', update: false },
    { arguments: ['avibe://settings'], resume: true, target: '/settings/general', update: false },
    { arguments: ['avibe://session/first', 'avibe://session/second'], resume: true, target: '/chat/first', update: false },
    { arguments: ['avibe://updates', 'avibe://session/explicit'], resume: true, target: '/chat/explicit', update: true },
    { arguments: ['avibe://updates'], resume: true, target: '/settings/computer-use', update: true },
    { arguments: [], resume: true, target: '/settings/computer-use', update: false },
    { arguments: ['--flag', 'https://invalid.example'], resume: true, target: '/settings/computer-use', update: false },
    { arguments: [], resume: false, target: null, update: false },
    { arguments: ['invalid'], resume: false, target: null, update: false },
    { arguments: ['avibe://session/explicit'], resume: false, target: '/chat/explicit', update: false },
  ];
  for (const policy of ['off', 'immediately']) for (const entry of cases) {
    await peer.send('reset', { policy });
    await peer.send('startup', entry);
    if (policy !== 'off') {
      expect(await peer.send('startupRead')).toEqual({ target: null, update: false });
      await peer.send('outcome', { value: 'success' }); await peer.send('unlock');
    }
    expect(await peer.send('startupRead')).toEqual({ target: entry.target, update: entry.update });
  }
});


test('all seven deferred checkmarks follow effective state after cancel and replacement', async () => {
  await peer.send('reset', { policy: 'immediately' });
  await peer.send('outcome', { value: 'cancelled' });
  type Menus = { items: Record<string, { saved: boolean; checked: boolean }>; files: Record<string, number[]> };
  const before = await peer.send<Menus>('checkmarks');
  expect(Object.keys(before.items)).toHaveLength(7);
  for (const click of Object.keys(before.items)) {
    await peer.send('checkmarks', { click });
    await expect.poll(() => peer.send('checkmarks')).toEqual(before);
    await peer.send('entry', { kind: 'command', value: 'replacement' });
    expect(await peer.send('checkmarks')).toEqual(before);
  }
  await peer.send('outcome', { value: 'success' }); await peer.send('unlock');
  expect(await peer.send('checkmarks')).toEqual(before);
  // The real Notifications preference is test-owned; replay toggles once.
  await peer.send('reset', { policy: 'immediately' });
  await peer.send('outcome', { value: 'cancelled' });
  await peer.send('checkmarks', { click: 'notifications' });
  await peer.send('outcome', { value: 'success' }); await peer.send('unlock');
  await peer.send('tick', { time: 1 });
  const after = await peer.send<Menus>('checkmarks');
  expect(after.items.notifications).toEqual({ saved: !before.items.notifications.saved, checked: !before.items.notifications.saved });
});
