import { expect, test, type Page } from '../support/browserTest';
import en from '../../src/i18n/en.json' with { type: 'json' };
import { open, serveProduct } from './support';

const SESSION = 'kept-voice-fixture';
const TAKE = 'offline-kept-take';
const WORDS = 'The same recording, recovered once.';

async function serveChat(page: Page) {
  const denied = await serveProduct(page);
  await page.route('**/api/session', (route) => route.fulfill({ json: {
    remote: false, authenticated: true, authorization_state: 'current',
    instance_kind: 'personal', instance_role: 'owner', author_id: 'local',
  } }));
  const session = {
    id: SESSION, title: 'Kept voice', agent_name: 'codex', agent_backend: 'codex',
    status: 'active', agent_status: 'idle', metadata: {}, scope_id: null, project_id: null,
  };
  const turn = {
    in_flight: false, foreground: 'idle', native_turn_started: false, pending_input_count: 0,
    background_activities: [], pending_activity_output_count: 0, connection: 'connected',
  };
  await page.route(`**/api/sessions/${SESSION}/bootstrap`, (route) => route.fulfill({ json: {
    session, capabilities: { can_chat: true },
    agents: [{ name: 'codex', backend: 'codex', enabled: true }], default_agent_name: 'codex',
    config: { ui: {} }, messages: [], queued: [], next_after_id: null, next_before_id: null,
    draft: { text: '', updated_at: null }, turn_state: turn,
  } }));
  await page.route(`**/api/sessions/${SESSION}`, (route) => route.fulfill({ json: session }));
  await page.route(`**/api/sessions/${SESSION}/queue`, (route) => route.fulfill({ json: [] }));
  await page.route(`**/api/sessions/${SESSION}/turn-state`, (route) => route.fulfill({ json: turn }));
  await page.route(`**/api/sessions/${SESSION}/activity**`, (route) => route.fulfill({ json: { groups: [] } }));
  await page.route(`**/api/sessions/${SESSION}/draft`, (route) => route.fulfill({ json: { ok: true } }));
  await page.route(`**/api/inbox/${SESSION}/read`, (route) => route.fulfill({ json: { ok: true } }));
  await page.route('**/api/asr/status', (route) => route.fulfill({ json: { available: true, consent: 'granted' } }));
  await page.route('**/api/asr/telemetry', (route) => route.fulfill({ json: { ok: true } }));
  await page.route('**/api/cloud/token', (route) => route.fulfill({ status: 503, json: {} }));
  return denied;
}

// Seed the released on-disk shape, as a different document retaining its take would.
// The real built store, BroadcastChannel consumer and composer must discover and consume it.
async function seedTake(page: Page) {
  await page.evaluate(async ({ session, id }) => {
    const request = indexedDB.open('avibe-voice-takes', 1);
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    const transaction = db.transaction(['takes', 'segments'], 'readwrite');
    transaction.objectStore('takes').put({
      id, target: session, intent: 'send', status: 'pending', principal: 'local',
      generation: Number(localStorage.getItem('avibe.sign-out-generation.v1') ?? 0),
      revision: 1, createdAt: Date.now(), updatedAt: Date.now(),
      segments: [{ sequence: 0, durationMs: 1000, final: true }],
    });
    transaction.objectStore('segments').put({
      id: [id, 0], take: id, audio: new Uint8Array([1, 2, 3, 4]).buffer,
    });
    await new Promise<void>((resolve, reject) => {
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error);
    });
    db.close();
    const channel = new BroadcastChannel('avibe-voice-takes');
    channel.postMessage('changed');
    channel.close();
  }, { session: SESSION, id: TAKE });
}

async function readTakes(page: Page) {
  return page.evaluate(async () => {
    if (!(await indexedDB.databases()).some((db) => db.name === 'avibe-voice-takes')) return null;
    const request = indexedDB.open('avibe-voice-takes', 1);
    const db = await new Promise<IDBDatabase>((resolve) => { request.onsuccess = () => resolve(request.result); });
    const rows = db.transaction('takes').objectStore('takes').getAll();
    const result = await new Promise<{ id: string; text?: string }[]>((resolve) => { rows.onsuccess = () => resolve(rows.result); });
    db.close();
    return result;
  });
}

test('first retained take stays usable offline without prewarming a card chunk', async ({ page, context }) => {
  const denied = await serveChat(page);
  // Idle prefetch never runs. This is a first appearance, not a cached card.
  await page.addInitScript(() => {
    window.requestIdleCallback = () => 0;
    window.cancelIdleCallback = () => undefined;
  });
  const errors: string[] = [];
  const scripts: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('request', (request) => {
    if (request.resourceType() === 'script') scripts.push(request.url());
  });
  const transcriptions: string[] = [];
  await page.route('**/api/asr/transcribe', (route) => {
    transcriptions.push(route.request().postDataJSON().dictation_id);
    return route.fulfill({ json: { text: WORDS, cleanup: 'success' } });
  });
  await open(page, `/chat/${SESSION}`);
  const editor = page.locator('main [contenteditable="true"]').last();
  await expect(editor).toBeVisible();
  await expect.poll(() => readTakes(page)).toEqual([]);
  const loadedScripts = [...scripts];
  expect(loadedScripts.some((url) => url.includes('PendingVoiceItem'))).toBe(false);

  await context.setOffline(true);
  await seedTake(page);
  const card = page.getByRole('status').filter({ hasText: en.voice.pending.title });
  await expect(card).toBeVisible();
  await expect(card.getByRole('button', { name: en.voice.retry })).toBeVisible();
  await editor.fill('Draft survives. ');
  await expect(editor).toHaveText('Draft survives. ');
  expect((await readTakes(page))?.map((take) => take.id)).toEqual([TAKE]);
  expect(transcriptions).toEqual([]);
  expect(scripts).toEqual(loadedScripts);
  expect(errors).toEqual([]);

  await context.setOffline(false);
  const ready = page.getByRole('status').filter({ hasText: en.voice.pending.ready });
  await expect(ready.getByText(WORDS)).toBeVisible();
  expect(transcriptions).toEqual([TAKE]);
  expect(await readTakes(page)).toMatchObject([{ id: TAKE, text: WORDS }]);
  // A subsequent visible/online event must not transcribe or duplicate the take.
  await page.evaluate(() => {
    window.dispatchEvent(new Event('online'));
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await ready.getByRole('button', { name: en.voice.pending.insert }).click();
  await expect(editor).toContainText(WORDS);
  await expect.poll(() => readTakes(page)).toEqual([]);
  await expect(ready).toHaveCount(0);
  expect(transcriptions).toEqual([TAKE]);
  expect(denied).toEqual([]);
  expect(errors).toEqual([]);
});
