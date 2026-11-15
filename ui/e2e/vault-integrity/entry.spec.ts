// AUTH-SETUP-137: intercepted synthetic addresses, real Vault consumers and entry gate.
import { writeFile } from 'node:fs/promises';
import { test, expect, type BrowserContext, type Page, type TestInfo } from '@playwright/test';
import type { VaultRequest, VaultSecret } from '../../src/context/ApiContext';
import en from '../../src/i18n/en.json' with { type: 'json' };
import zh from '../../src/i18n/zh.json' with { type: 'json' };

const server = 'http://127.0.0.1:5238';
const fixturePath = '/e2e/vault-integrity/entry-fixture.html';
const secret: VaultSecret = { name: 'PUBLIC_TEST_TOKEN', kind: 'static', protection: 'protected', source: 'user', signer_kind: null,
  tags: [], policy: {}, default_field: 'value', fields: ['value'], last_used_at: null, use_count: 0, created_at: '', updated_at: 'fixture-v1' };
const access: VaultRequest = { id: 'fixture-access', request_type: 'access', secret_name: secret.name, status: 'pending', requester: {},
  message_id: null, created_at: '', decided_at: null, expires_at: null, delivery: {},
  card: { request_type: 'access', protection: 'protected', secret_names: [secret.name], protected_secret_names: [secret.name],
    grant_options: [{ grant_id: 'fixture-grant', unlock_material: [{ name: secret.name,
      envelope: { ciphertext: 'invalid', nonce: 'invalid', wrap_meta: '{}' } }] }] } };
const sign: VaultRequest = { ...access, id: 'fixture-sign', request_type: 'sign',
  delivery: { digest: '00'.repeat(32), scheme: 'ecdsa-secp256k1-recoverable' },
  card: { request_type: 'sign', secret_name: 'PUBLIC_TEST_KEY', protection: 'protected', kind: 'keypair' } };
const standardSign = { ...sign, card: { ...sign.card, protection: 'standard' } };

type TimelineEvent = Record<string, unknown>;
type Timeline = {
  mark: (event: string) => void;
  observeAfterClick: () => void;
  dispose: () => void;
  attach: (testInfo: TestInfo) => Promise<void>;
};
const timelines = new WeakMap<Page, Timeline>();
const timelinePath = (url: string) => {
  const pathname = new URL(url).pathname;
  return pathname.replace(/\/secrets\/[^/]+/g, '/secrets/:name').replace(/\/requests\/[^/]+/g, '/requests/:id');
};

async function installTimeline(context: BrowserContext, page: Page, writes: string[]) {
  const startedAt = Date.now();
  const network: TimelineEvent[] = [];
  const recordNetwork = (event: TimelineEvent) => {
    if (network.length < 256) network.push({ source: 'network', at_ms: Date.now() - startedAt, ...event });
  };
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.startsWith('/api/')) {
      recordNetwork({ event: 'request', method: request.method(), path: timelinePath(request.url()) });
    }
  });
  page.on('response', (response) => {
    if (new URL(response.url()).pathname.startsWith('/api/')) {
      recordNetwork({ event: 'response', method: response.request().method(), path: timelinePath(response.url()), status: response.status() });
    }
  });
  page.on('requestfailed', (request) => {
    if (new URL(request.url()).pathname.startsWith('/api/')) {
      recordNetwork({ event: 'requestfailed', method: request.method(), path: timelinePath(request.url()) });
    }
  });
  // Both browser and runner use elapsed wall time from the same origin. No
  // bodies, headers, values, DOM text or exception messages enter this artifact.
  await context.addInitScript(({ startedAt }) => {
    type BrowserEvent = { source: 'browser'; at_ms: number; event: string; target: string | null; default_prevented?: boolean; save: Record<string, unknown> };
    const key = '__avibeVaultIntegrityTimeline';
    const events: BrowserEvent[] = [];
    const push = (event: string, target: EventTarget | null, defaultPrevented = false) => {
      if (events.length >= 256) return;
      const elementKind = (value: Element | null): string | null => {
        if (!value) return null;
        if (value instanceof HTMLButtonElement) return 'button';
        if (value instanceof HTMLFormElement) return 'form';
        if (value instanceof HTMLInputElement) return 'input';
        return value.tagName.toLowerCase();
      };
      const save = Array.from(document.querySelectorAll<HTMLButtonElement>('button[type="submit"]'))
        .find((button) => button.closest('[role="dialog"]')) ?? null;
      const rect = save?.getBoundingClientRect();
      const center = rect ? { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 } : null;
      const hit = center ? document.elementFromPoint(center.x, center.y) : null;
      events.push({
        source: 'browser',
        at_ms: Date.now() - startedAt,
        event,
        target: elementKind(target instanceof Element ? target : null),
        default_prevented: defaultPrevented,
        save: {
          present: Boolean(save),
          disabled: save?.disabled ?? null,
          focused: document.activeElement === save,
          display: save ? getComputedStyle(save).display : null,
          visibility: save ? getComputedStyle(save).visibility : null,
          pointer_events: save ? getComputedStyle(save).pointerEvents : null,
          rect: rect ? { x: rect.x, y: rect.y, width: rect.width, height: rect.height } : null,
          hit: elementKind(hit),
          hit_is_save: Boolean(save && hit && (hit === save || save.contains(hit))),
          active_element: elementKind(document.activeElement),
          form_valid: save?.form ? Array.from(save.form.elements).every((element) => !('validity' in element) || (element as HTMLInputElement).validity.valid) : null,
        },
      });
    };
    for (const event of ['pointerdown', 'pointerup', 'click', 'focusin', 'focusout', 'submit', 'invalid']) {
      // Capture survives React propagation guards; bubble submit separately
      // observes preventDefault, not proof that React passed its own guards.
      document.addEventListener(event, (value) => {
        if (value.target instanceof Element && value.target.closest('[role="dialog"]')) {
          push(`${event}:capture`, value.target, value.defaultPrevented);
        }
      }, true);
    }
    document.addEventListener('submit', (value) => push('submit:bubble', value.target, value.defaultPrevented));
    new MutationObserver((mutations) => {
      if (mutations.some((mutation) => mutation.target instanceof HTMLButtonElement && mutation.attributeName === 'disabled')) {
        push('save-disabled-change', document.querySelector('button[type="submit"]'));
      }
    }).observe(document, { subtree: true, attributes: true, attributeFilter: ['disabled'] });
    Object.defineProperty(window, key, { configurable: true, value: events });
  }, { startedAt });
  let observationTimer: ReturnType<typeof setTimeout> | undefined;
  const mark = (event: string) => recordNetwork({ event, writes: writes.map((key) => key.replace(secret.name, ':name')) });
  const timeline: Timeline = {
    mark,
    observeAfterClick: () => {
      mark('save-click-returned');
      // Preserve the old poll's observation point without reverting #3761's
      // response waits, sleeping, or changing any assertion/timeout.
      observationTimer = setTimeout(() => mark('five-seconds-after-click'), 5_000);
    },
    dispose: () => clearTimeout(observationTimer),
    attach: async (testInfo) => {
      let browser: unknown[];
      try {
        browser = await page.evaluate(() => (window as Window & { __avibeVaultIntegrityTimeline?: unknown[] }).__avibeVaultIntegrityTimeline ?? []);
      } catch {
        browser = [{ source: 'browser', event: 'timeline-read-failed' }];
      }
      // Body-only attachments need a reporter to persist them. Write under
      // outputDir too so the existing failure-artifact upload always includes it.
      const path = testInfo.outputPath('vault-integrity-event-timeline.json');
      await writeFile(path, JSON.stringify({ network, browser }, null, 2));
      await testInfo.attach('vault-integrity-event-timeline.json', {
        path,
        contentType: 'application/json',
      });
    },
  };
  timelines.set(page, timeline);
  return timeline;
}

test.afterEach(async ({ page }, testInfo) => {
  const timeline = timelines.get(page);
  timeline?.dispose();
  if (timeline && testInfo.status !== testInfo.expectedStatus) {
    timeline.mark('test-failed');
    await timeline.attach(testInfo);
  }
});

async function harness(context: BrowserContext, page: Page, origin: string, request = access) {
  const prohibited: string[] = [];
  const writes: string[] = [];
  const errors: string[] = [];
  const popups: Page[] = [];
  const documents: string[] = [];
  page.on('request', (req) => { if (req.resourceType() === 'document') documents.push(req.url()); });
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('popup', (popup) => popups.push(popup));
  await context.addInitScript(() => {
    // No physical credentials or installed browser state, even if a regression
    // gets past the entry gate. No sandbox resources are served by this harness.
    Object.defineProperty(navigator, 'credentials', { value: {
      create: () => { throw new Error('Credentials are forbidden'); },
      get: () => { throw new Error('Credentials are forbidden'); },
    } });
  });
  await context.routeWebSocket('**', (socket) => socket.close());
  await context.route('**/*', async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.origin !== origin) { prohibited.push(req.url()); return route.abort('blockedbyclient'); }
    if (url.pathname === '/settings/remote-access') { prohibited.push('document reload into Settings'); return route.abort('blockedbyclient'); }
    if (url.pathname.startsWith('/api/')) {
      const key = `${req.method()} ${url.pathname}`;
      const read: Record<string, unknown> = {
        '/api/session': { remote: false, instance_kind: 'personal' },
        '/api/vault/secrets': { ok: true, secrets: [secret] },
        '/api/vault/requests': { ok: true, requests: [request] },
        '/api/vault/grants': { ok: true, grants: [] },
        '/api/vault/settings': { ok: true, settings: {} },
        '/api/vault/avault-authorization': { ok: true, required: false, supported: false, attempt: null },
        '/api/dependencies': { ok: true, deps: [{ id: 'avault', status: 'ready', version: '0.1.6' }] },
        '/api/skills': { ok: true, skills: [] },
        '/api/csrf-token': { csrf_token: 'public-fixture-only' },
      };
      if (req.method() === 'GET' && url.pathname in read) return route.fulfill({ json: read[url.pathname] });
      if (req.method() === 'GET' && url.pathname.includes('/events')) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ': offline fixture\n\n' });
      if (key === `POST /api/vault/requests/${request.id}/deny` || key === 'POST /api/vault/sign') {
        writes.push(key); return route.fulfill({ json: { ok: true } });
      }
      // The edit dialog's one Save commits field changes first; a rename-only commit carries no
      // envelope, so it is allowed here while value writes are not.
      if (key === `POST /api/vault/secrets/${secret.name}/fields`) {
        const body = req.postDataJSON() as { default_field?: string; values?: unknown[]; remove?: unknown[] };
        if (!body.values?.length && !body.remove?.length) {
          writes.push(key);
          timelines.get(page)?.mark('field-write-recorded');
          const label = body.default_field ?? secret.default_field;
          return route.fulfill({ json: { ok: true, secret: { ...secret, default_field: label, fields: [label] } } });
        }
      }
      if (key === `PATCH /api/vault/secrets/${secret.name}`) {
        writes.push(key);
        timelines.get(page)?.mark('metadata-write-recorded');
        return route.fulfill({ json: { ok: true, secret: { ...secret, ...req.postDataJSON() } } });
      }
      // Preparation, settings mutation, credentials, grants and protected writes
      // are forbidden. Record before rejecting; a caught failure cannot hide it.
      prohibited.push(key);
      return route.fulfill({ status: 500, json: { ok: false, code: 'fixture-forbidden' } });
    }
    // The browser retains the synthetic origin, but only this test-owned Vite
    // server receives traffic. Never resolve/contact the LAN IP or public host.
    const response = await route.fetch({ url: `${server}${url.pathname}${url.search}`, maxRedirects: 0 });
    return route.fulfill({ response });
  });
  return { prohibited, writes, errors, popups, documents };
}

// Independent deep links belong to fresh built-in page/context fixtures, not
// successive loads of a live ApiProvider. Keep the same strict ledger on each.
async function expectSingleDocumentJourney(page: Page, observed: Awaited<ReturnType<typeof harness>>, url: string, writes: string[] = []) {
  await expect(page.locator('iframe')).toHaveCount(0);
  expect(observed.documents).toEqual([url]);
  expect(observed.writes).toEqual(writes);
  expect(observed.prohibited).toEqual([]);
  expect(observed.errors).toEqual([]);
  expect(observed.popups).toEqual([]);
}

// One pending request must survive Settings and return still blocked, then be denied.
// Denying first and resetting with goto created a second document (and SSE/session
// lifecycle); measuring identity only after that reset could not prove this journey.
for (const [origin, language, current] of [
  ['http://192.0.2.1:5123', 'zh', access],
  ['https://custom.example', 'en', sign],
  ['https://demo.avibe.bot:8443', 'en', access],
] as const) test(`early ${language} approval notice at ${origin}; deny and navigation remain usable`, async ({ context, page }) => {
  const copy = language === 'zh' ? zh : en;
  const observed = await harness(context, page, origin, current);
  await page.setViewportSize({ width: 390, height: 844 });
  const url = `${origin}${fixturePath}?lang=${language}&request_id=${current.id}`;
  await page.goto(url);
  const dialog = page.getByRole('dialog');
  const assertBlockedRequest = async () => {
    await expect(dialog.getByText(copy.vaults.entry.addressTitle)).toBeVisible();
    await expect(dialog.getByText(current.request_type === 'sign' ? 'PUBLIC_TEST_KEY' : secret.name, { exact: true }).first()).toBeVisible();
    if (current.request_type === 'sign') await expect(dialog.getByText('00'.repeat(32), { exact: true })).toBeVisible();
    await expect(dialog.getByRole('button', { name: current.request_type === 'sign' ? copy.vaults.approval.sign : copy.vaults.approval.approve, exact: true })).toBeDisabled();
    await expect(dialog.locator('.animate-spin')).toHaveCount(0);
    await expect(page.locator('iframe')).toHaveCount(0);
    expect(observed.prohibited).toEqual([]);
    expect(observed.writes).toEqual([]);
    expect(observed.errors).toEqual([]);
    expect(observed.popups).toEqual([]);
  };
  await assertBlockedRequest();
  await dialog.getByText(copy.vaults.entry.addressTitle).scrollIntoViewIfNeeded();
  await dialog.evaluate(async (element) => { await Promise.all(element.getAnimations().map((animation) => animation.finished.catch(() => undefined))); });
  await page.screenshot({ path: test.info().outputPath(`unsupported-${language}.png`) });
  const returnUrl = page.url();
  const documentIdentity = await page.evaluate(() => performance.timeOrigin);
  await page.getByRole('link', { name: copy.vaults.entry.remoteAccess }).click();
  await expect(page).toHaveURL(`${origin}/settings/remote-access`);
  await expect(page.getByRole('heading', { name: 'Remote Access destination fixture' })).toBeVisible();
  expect(await page.evaluate(() => performance.timeOrigin)).toBe(documentIdentity);
  await page.getByRole('button', { name: 'Back to Vaults' }).click();
  // The actual Settings surface retains this request, including on mobile.
  await expect(page).toHaveURL(returnUrl);
  expect(await page.evaluate(() => performance.timeOrigin)).toBe(documentIdentity);
  await assertBlockedRequest();
  // The only write identifies the very request we left open, after returning.
  await dialog.getByRole('button', { name: copy.vaults.approval.deny, exact: true }).click();
  await expect.poll(() => observed.writes).toEqual([`POST /api/vault/requests/${current.id}/deny`]);
  await expect(dialog).toHaveCount(0);
  await expectSingleDocumentJourney(page, observed, url, [`POST /api/vault/requests/${current.id}/deny`]);
});

test('unsupported reveal/edit stops before preparation, but metadata and cancel stay available', async ({ context, page }) => {
  const origin = 'http://192.0.2.1:5123';
  const observed = await harness(context, page, origin);
  const timeline = await installTimeline(context, page, observed.writes);
  const url = `${origin}${fixturePath}?reveal=${secret.name}`;
  await page.goto(url);
  await expect(page.getByRole('menu').getByText(en.vaults.entry.addressTitle)).toBeVisible();
  await expect(page.getByRole('menuitem', { name: en.vaults.reveal.show, exact: true })).toBeDisabled();
  await expect(page.getByRole('menuitem', { name: en.vaults.reveal.copy, exact: true })).toBeDisabled();
  page.setDefaultTimeout(8_000);
  await page.getByRole('menuitem', { name: en.vaults.editSecret }).click();
  expect(observed.errors).toEqual([]);
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText(en.vaults.entry.addressTitle)).toBeVisible();
  await dialog.getByLabel('Default field name').fill('password');
  await expect(dialog.getByLabel('Default field name')).toHaveValue('password');
  await expect(dialog.getByRole('button', { name: en.vaults.dialog.cancel, exact: true })).toBeEnabled();
  // One Save: the field change (a rename, no envelope) and then the metadata. The editor reports
  // the rename's validity to the dialog after it renders, so wait for Save to settle enabled.
  const saveButton = dialog.getByRole('button', { name: en.vaults.dialog.save, exact: true });
  await expect(saveButton).toBeEnabled();
  await expect(dialog.getByText(en.vaults.fields.editHelp)).toBeVisible();
  const saved = [`POST /api/vault/secrets/${secret.name}/fields`, `PATCH /api/vault/secrets/${secret.name}`];
  const fieldWrite = page.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname === `/api/vault/secrets/${secret.name}/fields`);
  const metadataWrite = page.waitForResponse((response) => response.request().method() === 'PATCH' && new URL(response.url()).pathname === `/api/vault/secrets/${secret.name}`);
  timeline.mark('save-click-start');
  await saveButton.click();
  timeline.observeAfterClick();
  await Promise.all([fieldWrite, metadataWrite]);
  expect(observed.writes).toEqual(saved);
  await expect(dialog).toHaveCount(0);
  await expectSingleDocumentJourney(page, observed, url, saved);
});

// A press on Save that starts while a field name still has focus. The press moves focus off the
// name; nothing above Save may move between the press and its release, or the release lands beside
// Save and the click is lost. Save is scrolled just into view, as a pointer click scrolls it (its
// bottom at the scroller's edge, so the dialog's padding stays below and a shorter body cannot pull
// the scroll back), the dialog's entrance has finished, and the press and release are split with the
// blur rendered between them, so the check does not depend on runner timing.
async function pressSaveFromFocusedName(page: Page, dialog: ReturnType<Page['getByRole']>, name: ReturnType<Page['getByRole']>) {
  const saveButton = dialog.getByRole('button', { name: en.vaults.dialog.save, exact: true });
  await expect(saveButton).toBeEnabled();
  await saveButton.evaluate((element) => element.scrollIntoView({ block: 'end' }));
  await dialog.evaluate(async (element) => { await Promise.all(element.getAnimations({ subtree: true }).map((animation) => animation.finished.catch(() => undefined))); });
  await expect(name).toBeFocused();
  const center = async () => {
    const box = await saveButton.boundingBox();
    if (!box) throw new Error('Save has no box');
    return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  };
  const pressed = await center();
  await page.mouse.move(pressed.x, pressed.y);
  await page.mouse.down();
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(name).not.toBeFocused();
  // Save stays where the press began (its pressed look scales it about its centre).
  expect(Math.abs((await center()).y - pressed.y)).toBeLessThan(1);
  await page.mouse.up();
}

for (const [label, edit, saved] of [
  ['an edited default field name', 'rename', [`POST /api/vault/secrets/${secret.name}/fields`, `PATCH /api/vault/secrets/${secret.name}`]],
  ['an empty new field', 'add', [`PATCH /api/vault/secrets/${secret.name}`]],
] as const) test(`a Save press that starts on ${label} still saves`, async ({ context, page }) => {
  const origin = 'http://192.0.2.1:5123';
  const observed = await harness(context, page, origin);
  const timeline = await installTimeline(context, page, observed.writes);
  const url = `${origin}${fixturePath}?reveal=${secret.name}`;
  await page.goto(url);
  page.setDefaultTimeout(8_000);
  await page.getByRole('menuitem', { name: en.vaults.editSecret }).click();
  const dialog = page.getByRole('dialog');
  let name = dialog.getByRole('textbox', { name: 'Default field name', exact: true });
  if (edit === 'rename') {
    await name.fill('password');
  } else {
    // A new field row left empty is not saved and does not block Save.
    await dialog.getByRole('button', { name: en.vaults.fields.add }).click();
    name = dialog.getByRole('textbox', { name: en.vaults.fields.newName, exact: true });
  }
  await expect(name).toBeFocused();
  await expect(dialog.getByText(en.vaults.fields.nameRule)).toBeVisible();
  const writes = saved.map((write) => page.waitForResponse((response) => `${response.request().method()} ${new URL(response.url()).pathname}` === write));
  timeline.mark('save-press-start');
  await pressSaveFromFocusedName(page, dialog, name);
  timeline.observeAfterClick();
  await Promise.all(writes);
  expect(observed.writes).toEqual(saved);
  await expect(dialog).toHaveCount(0);
  await expectSingleDocumentJourney(page, observed, url, [...saved]);
});

// A long chip draft (Tags, or Allowed hosts under Advanced) still focused when Save is pressed: the
// blur commits it, and the chip it becomes may wrap onto a new line. The press must still save, with
// the committed value.
const longTag = 'release-candidate-verification-tag-that-is-long-enough-to-wrap-the-row';
const longHost = 'a-long-subdomain-that-wraps-the-row.vault-integrity-fixture.example.com';
for (const [label, field] of [['Tags', 'tags'], ['Allowed hosts', 'hosts']] as const) test(`a Save press that starts on a long ${label} draft still saves it`, async ({ context, page }) => {
  const origin = 'http://192.0.2.1:5123';
  const observed = await harness(context, page, origin);
  const timeline = await installTimeline(context, page, observed.writes);
  const url = `${origin}${fixturePath}?reveal=${secret.name}`;
  await page.goto(url);
  page.setDefaultTimeout(8_000);
  await page.getByRole('menuitem', { name: en.vaults.editSecret }).click();
  const dialog = page.getByRole('dialog');
  if (field === 'hosts') await dialog.getByRole('button', { name: en.vaults.dialog.advanced }).click();
  // Tags is a combobox when it has suggestions to offer; this fixture has none, so both are text boxes.
  const input = dialog.getByRole('textbox', { name: field === 'tags' ? en.vaults.dialog.tags : en.vaults.dialog.allowHosts, exact: true });
  await input.fill(field === 'tags' ? longTag : longHost);
  await expect(input).toBeFocused();
  const saved = [`PATCH /api/vault/secrets/${secret.name}`];
  const patch = page.waitForRequest((request) => request.method() === 'PATCH' && new URL(request.url()).pathname === `/api/vault/secrets/${secret.name}`);
  timeline.mark('save-press-start');
  await pressSaveFromFocusedName(page, dialog, input);
  timeline.observeAfterClick();
  const body = (await patch).postDataJSON() as { tags?: string[]; policy?: { allowed_hosts?: string[] } };
  if (field === 'tags') expect(body.tags).toContain(longTag);
  else expect(body.policy?.allowed_hosts).toContain(longHost);
  await expect.poll(() => observed.writes).toEqual(saved);
  await expect(dialog).toHaveCount(0);
  await expectSingleDocumentJourney(page, observed, url, saved);
});

test('unsupported create preserves Settings return, draft clearing, Standard and cancel', async ({ context, page }) => {
  const origin = 'http://192.0.2.1:5123';
  const observed = await harness(context, page, origin);
  const url = `${origin}${fixturePath}?add=protected`;
  await page.goto(url);
  page.setDefaultTimeout(8_000);
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText(en.vaults.entry.addressTitle)).toBeVisible();
  await expect(dialog.getByRole('button', { name: en.vaults.dialog.createSecret, exact: true })).toBeDisabled();
  await dialog.getByPlaceholder(en.vaults.dialog.valuePlaceholder).fill('public-fixture-draft');
  await expect(dialog.getByText(en.vaults.entry.draftWarning)).toBeVisible();
  await dialog.getByRole('link', { name: en.vaults.entry.remoteAccess }).click();
  await expect(page.getByRole('heading', { name: 'Remote Access destination fixture' })).toBeVisible();
  expect(new URL(page.url()).search).toBe('');
  await page.getByRole('button', { name: 'Back to Vaults' }).click();
  // Existing DialogContent suspension unmounts the form (explicitly disclosed);
  // the dialog reopens, but secret drafts are never persisted or put in the URL.
  await expect(dialog.getByPlaceholder(en.vaults.dialog.valuePlaceholder)).toHaveValue('');
  await dialog.getByRole('button', { name: en.vaults.dialog.standardProtection, exact: false }).click();
  await expect(dialog.getByText(en.vaults.entry.addressTitle)).toHaveCount(0);
  await dialog.getByRole('button', { name: en.vaults.dialog.cancel, exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expectSingleDocumentJourney(page, observed, url);
});

test('supported HTTPS with missing Web Crypto gives browser advice, not a Remote Access enable prompt', async ({ context, page }) => {
  const origin = 'https://fixture.avibe.bot';
  await context.addInitScript(() => Object.defineProperty(crypto, 'subtle', { value: undefined }));
  const observed = await harness(context, page, origin);
  const url = `${origin}${fixturePath}?request_id=${access.id}`;
  await page.goto(url);
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText(en.vaults.entry.browserTitle)).toBeVisible();
  await expect(dialog.getByText(en.vaults.entry.browserHelp)).toBeVisible();
  await expect(dialog.getByRole('link', { name: en.vaults.entry.remoteAccess })).toHaveCount(0);
  await expect(dialog.getByRole('button', { name: en.vaults.approval.approve, exact: true })).toBeDisabled();
  await expectSingleDocumentJourney(page, observed, url);
});

test('non-owner protected guidance has no Remote Access link', async ({ context, page }) => {
  const origin = 'http://192.0.2.1:5123';
  const observed = await harness(context, page, origin, standardSign);
  const url = `${origin}${fixturePath}?role=member&reveal=${secret.name}`;
  await page.goto(url);
  await expect(page.getByText(en.vaults.entry.memberHelp)).toBeVisible();
  await expect(page.getByRole('link', { name: en.vaults.entry.remoteAccess })).toHaveCount(0);
  await expectSingleDocumentJourney(page, observed, url);
});

test('non-owner standard signing remains on the ordinary API path', async ({ context, page }) => {
  const origin = 'http://192.0.2.1:5123';
  const observed = await harness(context, page, origin, standardSign);
  const url = `${origin}${fixturePath}?role=member&request_id=${standardSign.id}`;
  await page.goto(url);
  await page.getByRole('dialog').getByRole('button', { name: en.vaults.approval.sign, exact: true }).click();
  await expect.poll(() => observed.writes).toEqual(['POST /api/vault/sign']);
  await expectSingleDocumentJourney(page, observed, url, ['POST /api/vault/sign']);
});
