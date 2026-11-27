import { expect, test, type Page } from '../support/browserTest';
import { open, serveProduct } from './support';

// A message sent from the composer and the page torn down before the instance
// answered (an iPhone App switched away at once): the composer emptied at Send,
// so the words live only on the device until the answer. The next page to open
// the chat gives back what the instance never received, as a draft and never sent
// by itself, and gives back nothing the instance holds.

const SESSION_ID = 'unacknowledged-send-fixture';
const ATTACHMENT = {
  token: 'upload-notes', name: 'notes.txt', mime: 'text/plain', size: 12, kind: 'file', url: '/api/media/upload-notes',
};

type Sent = { text: string; tokens: string[]; metadata: Record<string, string> };

/**
 * The instance's side of the chat, shared by every page a case opens. A send's
 * POST is recorded and never answered: the page that made it is gone first.
 * With ``receives`` the instance took the message before that (it is in the
 * conversation, and taking it cleared the saved draft, as the server's admission
 * does); without, it never arrived.
 */
function instance({ receives }: { receives: boolean }) {
  return {
    receives,
    principal: 'reader-a',
    held: [] as Array<Record<string, unknown>>,
    /** Rows above the chat's latest window: a page reads them only by paging back. */
    older: [] as Array<Record<string, unknown>>,
    sent: [] as Sent[],
    draft: { text: '', updated_at: null as string | null },
    revision: 0,
  };
}
type Instance = ReturnType<typeof instance>;

const saveDraft = (chat: Instance, text: string) => {
  chat.revision += 1;
  chat.draft = { text, updated_at: `2026-11-27T13:00:${String(chat.revision).padStart(2, '0')}Z` };
};

const readerAnswer = (principal: string) => ({
  remote: false, authenticated: true, authorization_state: 'current', instance_kind: 'personal',
  instance_role: 'owner', author_id: principal,
});

/** The instance takes a send: it is in the conversation, and the saved draft is cleared. */
function receive(chat: Instance, sent: Sent, content: Record<string, unknown> = {}) {
  saveDraft(chat, '');
  const row = {
    id: `msg-${chat.held.length + 1}`, scope_id: null, session_id: SESSION_ID, platform: 'avibe', author: 'user',
    type: 'user', source: 'user', author_id: chat.principal, author_name: null, native_message_id: null,
    parent_native_message_id: null, text: sent.text, content, metadata: sent.metadata,
    // Taken now, by the clock the page compares a send's time with.
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(), delivered_at: null, read_at: null,
  };
  chat.held.push(row);
  return row;
}

/** Answer one page's reads from ``chat``. */
async function serveChat(page: Page, chat: Instance) {
  const denied = await serveProduct(page);
  const session = {
    id: SESSION_ID, scope_id: 'fixture', project_id: 'proj-1', title: 'Unacknowledged send',
    agent_id: 'agt-codex', agent_name: 'codex', agent_backend: 'codex', model: null,
    reasoning_effort: null, status: 'active', agent_status: 'idle', pinned: false,
    workdir: '/fixture/work', native_session_id: null, metadata: {},
    created_at: '2026-11-27T00:00:00Z', updated_at: '2026-11-27T00:00:00Z',
  };
  const turnState = {
    in_flight: false, foreground: 'idle', native_turn_started: false,
    pending_input_count: 0, background_activities: [], pending_activity_output_count: 0, connection: 'connected',
  };
  // Records belong to the reader the instance names here.
  await page.route('**/api/session', (route) => route.fulfill({ json: readerAnswer(chat.principal) }));
  await page.route(`**/api/sessions/${SESSION_ID}/bootstrap`, (route) => route.fulfill({ json: {
    session, capabilities: { can_chat: true },
    agents: [{ id: 'agt-codex', name: 'codex', backend: 'codex', enabled: true }],
    default_agent_name: 'codex', config: { ui: {} },
    messages: chat.held, queued: [], next_after_id: null, next_before_id: chat.older.length ? 'msg-window-start' : null,
    draft: chat.draft, turn_state: turnState,
  } }));
  await page.route(`**/api/sessions/${SESSION_ID}`, (route) => route.fulfill({ json: session }));
  await page.route(`**/api/sessions/${SESSION_ID}/queue`, (route) => route.fulfill({ json: { queued: [] } }));
  await page.route(`**/api/sessions/${SESSION_ID}/turn-state`, (route) => route.fulfill({ json: turnState }));
  await page.route(`**/api/sessions/${SESSION_ID}/activity**`, (route) => route.fulfill({ json: { groups: [] } }));
  await page.route(`**/api/sessions/${SESSION_ID}/draft`, (route) => {
    if (route.request().method() === 'PUT') saveDraft(chat, (route.request().postDataJSON() as { text: string }).text);
    return route.fulfill({ json: { ok: true, draft: chat.draft } });
  });
  await page.route(`**/api/inbox/${SESSION_ID}/read`, (route) => route.fulfill({ json: { ok: true } }));
  await page.route(`**/api/sessions/${SESSION_ID}/messages**`, (route) => {
    const request = route.request();
    if (request.method() !== 'POST') {
      const params = new URL(request.url()).searchParams;
      const before = params.get('before_id');
      // A deep link's window around an older row: newer rows lie beyond it.
      if (params.get('around_id')) {
        return route.fulfill({ json: { messages: chat.older, next_before_id: null, next_after_id: 'msg-window-start', anchor_id: params.get('around_id') } });
      }
      return route.fulfill({ json: before === 'msg-window-start'
        ? { messages: chat.older, next_before_id: null, next_after_id: null }
        : { messages: chat.held, next_before_id: chat.older.length ? 'msg-window-start' : null, next_after_id: null } });
    }
    const body = request.postDataJSON() as {
      text: string; content?: { attachments?: Array<{ token: string }> }; metadata: Record<string, string>;
    };
    const sent = {
      text: body.text, tokens: (body.content?.attachments ?? []).map((attachment) => attachment.token), metadata: body.metadata,
    };
    chat.sent.push(sent);
    if (chat.receives) receive(chat, sent, body.content ?? {});
    // No answer: the page is torn down while this is out.
    return undefined;
  });
  return denied;
}

/** Open the chat in a new page of the same browser, as the App does after its page was torn down. */
async function reopen(page: Page, chat: Instance, denied: string[], serveMore?: (next: Page) => Promise<void>) {
  const next = await page.context().newPage();
  denied.push(...await serveChat(next, chat));
  await serveMore?.(next);
  await next.setViewportSize({ width: 1200, height: 800 });
  // Returned once the chat's bootstrap has answered: only then can a page give anything back.
  const booted = next.waitForResponse(`**/api/sessions/${SESSION_ID}/bootstrap`);
  await open(next, `/chat/${SESSION_ID}`);
  await booted;
  return next;
}

/** A restore pass runs shortly after the bootstrap: long enough for one, and for a draft save (600 ms). */
const RESTORE_SETTLED_MS = 1_200;

/** End a page the way the system ends a suspended App's: at once, running none of its code. */
async function tearDown(page: Page) {
  await page.close({ runBeforeUnload: false });
}

const composer = (page: Page) => page.locator('main [data-composer-root]');
const editor = (page: Page) => composer(page).locator('[contenteditable="true"]');
const sendButton = (page: Page) => composer(page).getByRole('button', { name: 'Send', exact: true });
const chip = (page: Page) => composer(page).getByText('notes.txt');
const records = (page: Page) => page.evaluate(() => Object.keys(window.localStorage)
  .filter((key) => key.startsWith('avibe.unacknowledged-send.')));

/** Open the chat with a file already uploaded into its draft. */
async function openWithAttachment(page: Page) {
  await page.setViewportSize({ width: 1200, height: 800 });
  await page.addInitScript(([id, attachment]) => {
    // Only into a fresh device: a later page must find what the earlier one left.
    if (window.sessionStorage.getItem('fixture.seeded')) return;
    window.sessionStorage.setItem('fixture.seeded', '1');
    window.localStorage.setItem(
      `avibe.session-draft-attachments.v1.${id}`,
      JSON.stringify({ version: 1, generation: 1, attachments: [attachment] }),
    );
  }, [SESSION_ID, ATTACHMENT] as const);
  await open(page, `/chat/${SESSION_ID}`);
  await expect(chip(page)).toHaveCount(1);
}

/** Type and send; the answer never comes. */
async function send(page: Page, chat: Instance, text: string) {
  await editor(page).click();
  await page.keyboard.type(text);
  await sendButton(page).click();
  await expect.poll(() => chat.sent.length).toBe(1);
  await expect(editor(page)).toHaveText('');
}

test('a send the instance never received comes back as a draft with its attachment, and is not sent again', async ({ page }) => {
  const chat = instance({ receives: false });
  const denied = await serveChat(page, chat);
  await openWithAttachment(page);

  await send(page, chat, 'Ship the release notes');
  await tearDown(page);
  expect(chat.sent).toMatchObject([{ text: 'Ship the release notes', tokens: ['upload-notes'] }]);
  const next = await reopen(page, chat, denied);

  await expect(editor(next)).toHaveText('Ship the release notes');
  await expect(chip(next)).toHaveCount(1);
  await expect(sendButton(next)).toBeEnabled();
  // Given back once: it is the chat's draft now, and the next page finds just that.
  await expect.poll(() => chat.draft.text).toBe('Ship the release notes');
  await tearDown(next);
  const third = await reopen(next, chat, denied);
  await expect(editor(third)).toHaveText('Ship the release notes');
  await expect(chip(third)).toHaveCount(1);
  expect(await records(third)).toEqual([]);
  expect(chat.sent).toHaveLength(1);
  expect(denied).toEqual([]);
});

test('a send the instance received before the page went shows in the conversation and not in the composer', async ({ page }) => {
  const chat = instance({ receives: true });
  const denied = await serveChat(page, chat);
  await openWithAttachment(page);

  await send(page, chat, 'Ship the release notes');
  await tearDown(page);
  const next = await reopen(page, chat, denied);

  await expect(next.locator('main').getByText('Ship the release notes')).toBeVisible();
  await expect(sendButton(next)).toBeVisible();
  await expect(editor(next)).toHaveText('');
  await expect(chip(next)).toHaveCount(0);
  expect(await records(next)).toEqual([]);
  expect(chat.sent).toHaveLength(1);
  expect(denied).toEqual([]);
});

test('a received send pushed out of the latest window by later rows is found further back, not given back', async ({ page }) => {
  const chat = instance({ receives: true });
  const denied = await serveChat(page, chat);
  await page.setViewportSize({ width: 1200, height: 800 });
  await open(page, `/chat/${SESSION_ID}`);

  await send(page, chat, 'Received long ago');
  await tearDown(page);
  // A long turn followed: the message now sits above the window a chat opens with.
  chat.older = chat.held.splice(0);
  chat.held.push({
    ...chat.older[0], id: 'msg-later', author: 'agent', type: 'result', source: 'agent', author_id: null,
    text: 'A later answer', metadata: {}, created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  });
  const next = await reopen(page, chat, denied);

  await expect(next.locator('main').getByText('A later answer')).toBeVisible();
  await expect.poll(() => records(next)).toEqual([]);
  await expect(editor(next)).toHaveText('');
  expect(chat.sent).toHaveLength(1);
  expect(denied).toEqual([]);
});

test('a received send is found in the latest rows when the chat opens on an older stretch', async ({ page }) => {
  const chat = instance({ receives: true });
  const denied = await serveChat(page, chat);
  await page.setViewportSize({ width: 1200, height: 800 });
  await open(page, `/chat/${SESSION_ID}`);

  await send(page, chat, 'Received, newer than the link');
  await tearDown(page);
  // Opened from a link to an earlier message, and the reader is confirmed only
  // after that window is on screen.
  chat.older = [{
    ...chat.held[0], id: 'msg-linked', author: 'agent', type: 'result', source: 'agent', author_id: null,
    text: 'The linked answer', metadata: {}, created_at: '2026-11-01T00:00:00Z', updated_at: '2026-11-01T00:00:00Z',
  }];
  let confirm!: () => void;
  const confirmed = new Promise<void>((resolve) => { confirm = resolve; });
  const next = await page.context().newPage();
  denied.push(...await serveChat(next, chat));
  await next.route('**/api/session', async (route) => {
    await confirmed;
    await route.fulfill({ json: readerAnswer(chat.principal) });
  });
  await next.setViewportSize({ width: 1200, height: 800 });
  await open(next, `/chat/${SESSION_ID}?msg=msg-linked`);
  await expect(next.locator('main').getByText('The linked answer')).toBeVisible();
  confirm();

  await expect.poll(() => records(next)).toEqual([]);
  await next.waitForTimeout(RESTORE_SETTLED_MS);
  await expect(editor(next)).toHaveText('');
  expect(chat.sent).toHaveLength(1);
  expect(denied).toEqual([]);
});

test('two pages opening together give a send back once', async ({ page }) => {
  const chat = instance({ receives: false });
  const denied = await serveChat(page, chat);
  await page.setViewportSize({ width: 1200, height: 800 });
  await open(page, `/chat/${SESSION_ID}`);

  await send(page, chat, 'Only once');
  await tearDown(page);
  // Both pages check the instance at the same moment: their reads of its latest
  // rows are held until each has asked, then answered together.
  const asked = new Set<Page>();
  let release!: () => void;
  const released = new Promise<void>((resolve) => { release = resolve; });
  const holdLatestRows = async (opened: Page) => {
    await opened.route(`**/api/sessions/${SESSION_ID}/messages**`, async (route) => {
      if (route.request().method() !== 'GET' || !new URL(route.request().url()).searchParams.get('tail')) return route.fallback();
      asked.add(opened);
      await released;
      return route.fallback();
    });
  };
  const [first, second] = await Promise.all([
    reopen(page, chat, denied, holdLatestRows),
    reopen(page, chat, denied, holdLatestRows),
  ]);
  await expect.poll(() => asked.size).toBe(2);
  release();
  await first.waitForTimeout(RESTORE_SETTLED_MS);

  // Each page's composer was empty when it loaded: only the page that gave it back shows it.
  const given = await Promise.all([first, second].map((opened) => editor(opened).innerText()));
  expect(given.map((text) => text.trim()).sort()).toEqual(['', 'Only once']);
  expect(await records(first)).toEqual([]);
  expect(denied).toEqual([]);
});

test('a send is never given back to another reader, even while the page still names the last one', async ({ page }) => {
  const chat = instance({ receives: false });
  const denied = await serveChat(page, chat);
  await page.setViewportSize({ width: 1200, height: 800 });
  await open(page, `/chat/${SESSION_ID}`);

  await send(page, chat, 'Only for the one who wrote it');
  expect(await records(page)).toHaveLength(1);
  await tearDown(page);
  // The next page on this device is someone else's. It starts from the boot
  // session it stored, which names the last reader, until the instance answers
  // who reads now, after the chat itself has loaded.
  chat.principal = 'reader-b';
  let confirm!: () => void;
  const confirmed = new Promise<void>((resolve) => { confirm = resolve; });
  const next = await reopen(page, chat, denied, async (opened) => {
    await opened.route('**/api/session', async (route) => {
      await confirmed;
      await route.fulfill({ json: readerAnswer(chat.principal) });
    });
  });

  // Loaded, still on the replayed reader: nothing comes back.
  await next.waitForTimeout(RESTORE_SETTLED_MS);
  await expect(editor(next)).toHaveText('');
  confirm();
  await expect.poll(() => records(next)).toEqual([]);
  await next.waitForTimeout(RESTORE_SETTLED_MS);
  await expect(editor(next)).toHaveText('');
  expect(chat.draft.text).toBe('');
  expect(denied).toEqual([]);
});

test('a send still out in another tab is not given back there', async ({ page }) => {
  const chat = instance({ receives: false });
  const denied = await serveChat(page, chat);
  await page.setViewportSize({ width: 1200, height: 800 });
  await open(page, `/chat/${SESSION_ID}`);

  await send(page, chat, 'Still on its way');
  // The sending page stays open, waiting for the answer.
  const other = await reopen(page, chat, denied);
  await other.waitForTimeout(RESTORE_SETTLED_MS);
  await expect(editor(other)).toHaveText('');
  expect(await records(other)).toHaveLength(1);
  // Once the sending page is gone, the next page gives it back.
  await tearDown(page);
  await tearDown(other);
  const third = await reopen(other, chat, denied);
  await expect(editor(third)).toHaveText('Still on its way');
  expect(chat.sent).toHaveLength(1);
  expect(denied).toEqual([]);
});

test('a draft given back leaves the composer when the conversation shows the message after all', async ({ page }) => {
  const chat = instance({ receives: false });
  const denied = await serveChat(page, chat);
  await page.setViewportSize({ width: 1200, height: 800 });
  await open(page, `/chat/${SESSION_ID}`);

  await send(page, chat, 'Taken after the page went');
  await tearDown(page);
  // The instance takes the send only after the next page has read the chat; the
  // live stream brings it then.
  let release!: () => void;
  const released = new Promise<void>((resolve) => { release = resolve; });
  const next = await reopen(page, chat, denied, async (opened) => {
    let streamed = false;
    await opened.route('**/api/events**', async (route) => {
      const first = !streamed;
      streamed = true;
      if (first) await released;
      const row = first ? receive(chat, chat.sent[0]) : null;
      return route.fulfill({
        status: 200,
        headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' },
        body: row ? `event: message.new\ndata: ${JSON.stringify({ type: 'message.new', data: row })}\n\n` : ': idle\n\n',
      });
    });
  });
  await expect(editor(next)).toHaveText('Taken after the page went');

  release();
  await expect(next.locator('main').getByText('Taken after the page went')).toHaveCount(1);
  await expect(editor(next)).toHaveText('');
  await expect.poll(() => chat.draft.text).toBe('');
  expect(chat.sent).toHaveLength(1);
  expect(denied).toEqual([]);
});

test('a draft given back on an older stretch leaves the composer when the stream brings the message', async ({ page }) => {
  const chat = instance({ receives: false });
  const denied = await serveChat(page, chat);
  await page.setViewportSize({ width: 1200, height: 800 });
  await open(page, `/chat/${SESSION_ID}`);

  await send(page, chat, 'Taken while reading back');
  await tearDown(page);
  // Opened from a link to an earlier answer: the page shows that stretch, not the
  // live rows, so it draws no message the stream brings.
  chat.older = [{
    id: 'msg-linked', scope_id: null, session_id: SESSION_ID, platform: 'avibe', author: 'agent', type: 'result',
    source: 'agent', author_id: null, author_name: null, native_message_id: null, parent_native_message_id: null,
    text: 'The linked answer', content: {}, metadata: {}, created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z', delivered_at: null, read_at: null,
  }];
  chat.held.push({ ...chat.older[0], id: 'msg-latest', text: 'The latest answer', created_at: new Date().toISOString() });
  let release!: () => void;
  const released = new Promise<void>((resolve) => { release = resolve; });
  const next = await page.context().newPage();
  denied.push(...await serveChat(next, chat));
  let streamed = false;
  await next.route('**/api/events**', async (route) => {
    const first = !streamed;
    streamed = true;
    if (first) await released;
    const row = first ? receive(chat, chat.sent[0]) : null;
    return route.fulfill({
      status: 200,
      headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' },
      body: row ? `event: message.new\ndata: ${JSON.stringify({ type: 'message.new', data: row })}\n\n` : ': idle\n\n',
    });
  });
  await next.setViewportSize({ width: 1200, height: 800 });
  await open(next, `/chat/${SESSION_ID}?msg=msg-linked`);
  await expect(next.locator('main').getByText('The linked answer')).toBeVisible();
  await expect(editor(next)).toHaveText('Taken while reading back');

  release();
  await expect(editor(next)).toHaveText('');
  expect(chat.sent).toHaveLength(1);
  expect(denied).toEqual([]);
});
