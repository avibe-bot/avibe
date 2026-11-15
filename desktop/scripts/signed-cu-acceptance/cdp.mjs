// Actual installed WebView2 automation. No direct Tauri invoke, screenshots,
// external packages, browser replacement, or installed-code modification.
import { parseArgs } from 'node:util';

const { values } = parseArgs({ options: {
  port: { type: 'string' }, origin: { type: 'string' }, action: { type: 'string' },
} });
const origin = new URL(values.origin);
if (origin.protocol !== 'http:' || origin.hostname !== '127.0.0.1' || origin.username || origin.password) {
  throw new Error('fixture_loopback_origin_required');
}
if (!['inspect', 'enable'].includes(values.action)) throw new Error('fixture_action_required');
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let socket;
let nextId = 1;
const pending = new Map();
function command(method, params) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('cdp_command_timeout')); }, 5000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const answer = await command('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (answer.exceptionDetails) throw new Error('cdp_evaluation_failed');
  return answer.result?.value;
}
try {
  const response = await fetch(`http://127.0.0.1:${Number(values.port)}/json/list`, { signal: AbortSignal.timeout(5000) });
  const targets = await response.json();
  // Only the actual Workbench target, not a worker/iframe or unrelated page.
  const target = targets.find((item) => item.type === 'page' && item.url.startsWith(origin.origin + '/'));
  if (!target) throw new Error('cdp_workbench_target_missing');
  const endpoint = new URL(target.webSocketDebuggerUrl);
  if (endpoint.protocol !== 'ws:' || !['127.0.0.1', 'localhost', '[::1]'].includes(endpoint.hostname) || Number(endpoint.port) !== Number(values.port)) {
    throw new Error('cdp_endpoint_not_local');
  }
  socket = new WebSocket(endpoint);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('cdp_attach_timeout')), 5000);
    socket.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
    socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('cdp_attach_failed')); }, { once: true });
  });
  socket.addEventListener('message', ({ data }) => {
    const message = JSON.parse(data);
    const request = pending.get(message.id);
    if (!request) return;
    pending.delete(message.id); clearTimeout(request.timer);
    if (message.error) request.reject(new Error('cdp_protocol_error'));
    else request.resolve(message.result);
  });
  const destination = origin.origin + '/settings/computer-use';
  await evaluate(`location.assign(${JSON.stringify(destination)}); true`);
  const stateExpression = `(() => {
    const status = document.querySelector('[data-testid="computer-use-guide-status"]');
    const button = document.querySelector('[data-testid="computer-use-action-enable"]');
    return {
      status: status?.getAttribute('data-computer-use-status') ?? null,
      unsupported: !!document.querySelector('[data-testid="computer-use-unsupported"]'),
      preview: !!document.querySelector('[data-testid="computer-use-preview-badge"]'),
      enable: !!button && !button.disabled,
      failed: !!document.querySelector('[data-testid="computer-use-action-failed"]'),
      shell: !!window.__AVIBE_DESKTOP_COMPUTER_USE__
    };
  })()`;
  let state;
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    try { state = await evaluate(stateExpression); } catch { await pause(250); continue; }
    if (state.unsupported || (state.status && state.status !== 'loading')) break;
    await pause(250);
  }
  if (!state || (!state.unsupported && !state.status)) throw new Error('settings_page_not_ready');
  if (values.action === 'enable') {
    if (state.status !== 'off' || !state.preview || !state.enable || !state.shell) throw new Error('settings_default_off_preview_not_proven');
    const clicked = await evaluate(`(() => { const b = document.querySelector('[data-testid="computer-use-action-enable"]'); if (!b || b.disabled) return false; b.click(); return true; })()`);
    if (!clicked) throw new Error('settings_button_not_clicked');
    while (Date.now() < deadline) {
      state = await evaluate(stateExpression);
      if (state.failed) throw new Error('settings_enable_action_failed');
      if (state.status === 'ready' && !state.enable) break;
      await pause(250);
    }
    if (state.status !== 'ready' || state.enable) throw new Error('settings_enable_not_ready');
  }
  console.log(JSON.stringify({ ok: true, ...state }));
} catch (error) {
  const reason = typeof error.message === 'string' && /^[a-z][a-z0-9_]+$/.test(error.message) ? error.message : 'cdp_fixture_failed';
  console.log(JSON.stringify({ ok: false, reason }));
  process.exitCode = 1;
} finally {
  for (const request of pending.values()) clearTimeout(request.timer);
  socket?.close();
}
