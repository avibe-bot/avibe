// Exercise of #3950 on the worktree regression environment (local origin). Headless safe browser:
// clipboard and native IPC stubbed, no focus. A real mouse click on desktop, a CDP touch tap on phone.
import { launchSafeBrowser, assertSafePage } from './safe-browser.mjs';

const origin = 'http://127.0.0.1:15204';
const out = '/Users/cyh/avibe-project/_tmp/approval-inbox/regr3950';
const thread = `${origin}/chat/ses-workspace-notices`;
const log = {};
const row = (page, id) => page.locator(`[data-message-id="${id}"]`);
const notices = async () => (await (await fetch(`${origin}/api/sessions/ses-workspace-notices/messages?limit=20`)).json()).messages;

const browser = await launchSafeBrowser({ contextOptions: { viewport: { width: 1360, height: 900 }, locale: 'zh-CN' } });
try {
  const [first, second] = (await notices()).filter((m) => m.content?.failure_source?.retry && !m.content?.failure_retry);
  // ── Desktop, real mouse: 重试, then 打开会话 ──
  const desk = await browser.context.newPage();
  await desk.goto(thread);
  await assertSafePage(desk);
  const r1 = row(desk, first.id);
  await desk.waitForTimeout(4000);
  await desk.screenshot({ path: `${out}/desktop-before.png` });
  await r1.getByRole('button', { name: /^(重试|Retry)$/ }).waitFor({ timeout: 30000 });
  const b = await r1.getByRole('button', { name: /^(重试|Retry)$/ }).boundingBox();
  await desk.mouse.click(b.x + b.width / 2, b.y + b.height / 2);
  await r1.getByRole('button', { name: /已请求重试|Retry requested/ }).waitFor({ timeout: 15000 });
  log.desktopStartedDisabled = await r1.getByRole('button', { name: /已请求重试|Retry requested/ }).isDisabled();
  await desk.screenshot({ path: `${out}/desktop-after-retry.png` });
  await desk.reload();
  await row(desk, first.id).getByRole('button', { name: /已请求重试|Retry requested/ }).waitFor({ timeout: 30000 });
  log.desktopStartedAfterReload = true;
  const o = await row(desk, first.id).getByRole('link', { name: /打开会话|Open session/ }).boundingBox();
  await desk.mouse.click(o.x + o.width / 2, o.y + o.height / 2);
  await desk.waitForURL(new RegExp(`/chat/${first.content.failure_source.session_id}`), { timeout: 15000 });
  log.desktopOpened = desk.url();
  await desk.waitForTimeout(3000);
  await desk.screenshot({ path: `${out}/desktop-opened-session.png` });
  await desk.close();

  // ── Phone width, real touch: 重试 ──
  const phone = await browser.context.newPage();
  await phone.setViewportSize({ width: 390, height: 844 });
  const cdp = await browser.context.newCDPSession(phone);
  await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 });
  await phone.goto(thread);
  await assertSafePage(phone);
  const r2 = row(phone, second.id);
  await r2.getByRole('button', { name: /^(重试|Retry)$/ }).waitFor({ timeout: 30000 });
  await r2.scrollIntoViewIfNeeded();
  const t = await r2.getByRole('button', { name: /^(重试|Retry)$/ }).boundingBox();
  const point = { x: t.x + t.width / 2, y: t.y + t.height / 2 };
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [point] });
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  await r2.getByRole('button', { name: /已请求重试|Retry requested/ }).waitFor({ timeout: 15000 });
  log.phoneStartedDisabled = await r2.getByRole('button', { name: /已请求重试|Retry requested/ }).isDisabled();
  await phone.screenshot({ path: `${out}/phone-after-retry.png` });
  log.server = (await notices()).filter((m) => [first.id, second.id].includes(m.id)).map((m) => [m.id, m.content.failure_source, m.content.failure_retry?.state]);
} finally {
  console.log(JSON.stringify(log, null, 1));
  await browser.close();
}
