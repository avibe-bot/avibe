// Actual installed WebView2 automation. No direct Tauri invoke, screenshots,
// external packages, browser replacement, or installed-code modification.
import { parseArgs } from 'node:util';

const { values } = parseArgs({ options: {
  port: { type: 'string' }, origin: { type: 'string' }, action: { type: 'string' },
  'current-version': { type: 'string' }, 'target-version': { type: 'string' },
} });
const origin = new URL(values.origin);
if (origin.protocol !== 'http:' || origin.hostname !== '127.0.0.1' || origin.username || origin.password) {
  throw new Error('fixture_loopback_origin_required');
}
if (!['inspect', 'enable', 'update-inspect', 'update-start', 'update-verify'].includes(values.action)) throw new Error('fixture_action_required');
const updateAction = values.action.startsWith('update-');
if (updateAction && !/^3\.2\.7rc\d+$/.test(values['current-version'] ?? '')) throw new Error('update_current_version_required');
if (values.action === 'update-start' && !/^3\.2\.7rc\d+$/.test(values['target-version'] ?? '')) throw new Error('update_target_version_required');
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
async function navigateTo(url) {
  await command('Page.enable', {});
  const navigation = await command('Page.navigate', { url });
  if (navigation.errorText || !navigation.loaderId) throw new Error('cdp_navigation_unproven');
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const tree = await command('Page.getFrameTree', {});
    if (tree.frameTree?.frame?.loaderId === navigation.loaderId) {
      try { if (await evaluate('document.readyState === "complete"')) return; } catch { /* new execution context */ }
    }
    await pause(100);
  }
  throw new Error('cdp_navigation_not_complete');
}
async function updatePanel() {
  const current = values['current-version'];
  const targetVersion = values['target-version'];
  const normalize = (text) => text.trim().replace(/^v/, '').replace(/-rc\./, 'rc');
  await navigateTo(origin.origin + '/');
  let opened = false;
  const deadline = Date.now() + 60000;
  while (Date.now() < deadline) {
    try {
      opened = await evaluate(`(() => {
        const normalize = ${normalize.toString()};
        const buttons = [...document.querySelectorAll('button[aria-controls]')]
          .filter(e => e.getClientRects().length && normalize(e.textContent ?? '') === ${JSON.stringify(current)});
        if (buttons.length !== 1) return false;
        buttons[0].click(); return true;
      })()`);
    } catch { /* Wait for the actual Workbench, not its navigation placeholder. */ }
    if (opened) break;
    await pause(250);
  }
  if (!opened) throw new Error('update_version_badge_click_unproven');
  const panelExpression = `(() => {
    const workbench = location.origin === ${JSON.stringify(origin.origin)};
    const trusted = location.origin === 'http://tauri.localhost' && location.pathname === '/account/index.html';
    if (!workbench && !trusted) return null;
    const panels = [...document.querySelectorAll('[data-update-panel]')].filter(e => e.getClientRects().length);
    if (panels.length !== 1) return null;
    const panel = panels[0];
    const normalize = ${normalize.toString()};
    const label = [...panel.querySelectorAll('span')].find(e => e.textContent.trim() === 'Current version');
    const current = label?.nextElementSibling?.textContent;
    const offered = panel.querySelector('a[href*="/releases/"] span')?.textContent;
    const setting = name => panel.querySelector('[role="switch"][aria-label="' + name + '"]');
    const state = element => element?.getAttribute('aria-checked') === 'true' ? true : element?.getAttribute('aria-checked') === 'false' ? false : null;
    const check = panel.querySelector('button[aria-label="Check for updates"]');
    return { panel_view: trusted ? 'trusted_shell' : 'workbench', current: current ? normalize(current) : null, offered: offered ? normalize(offered) : null,
      status: panel.getAttribute('data-update-status'), auto_update: state(setting('Update automatically')),
      prereleases: state(setting('Get pre-release updates')), check_enabled: !!check && !check.disabled };
  })()`;
  let state;
  while (Date.now() < deadline) {
    try { state = await evaluate(panelExpression); } catch { state = null; }
    if (state?.current === current && typeof state.auto_update === 'boolean' && typeof state.prereleases === 'boolean') break;
    await pause(250);
  }
  if (state?.current !== current) throw new Error('update_current_version_row_unproven');
  if (typeof state.auto_update !== 'boolean' || typeof state.prereleases !== 'boolean') throw new Error('update_settings_unreadable');
  const automatic = state.auto_update;
  if (values.action === 'update-verify') return { ok: true, badge_clicked: true, current_row_verified: true, ...state };
  if (values.action === 'update-start' && !state.prereleases) {
    const toggled = await evaluate(`(() => {
      const e = document.querySelector('[data-update-panel] [role="switch"][aria-label="Get pre-release updates"]');
      if (!e || e.disabled || e.getAttribute('aria-checked') !== 'false') return false;
      e.click(); return true;
    })()`);
    if (!toggled) throw new Error('update_prerelease_switch_click_unproven');
  }
  while (Date.now() < deadline) {
    state = await evaluate(panelExpression);
    if (state?.check_enabled && (values.action !== 'update-start' || state.prereleases)) break;
    await pause(250);
  }
  if (!state?.check_enabled) throw new Error('update_check_button_unavailable');
  if (state.auto_update !== automatic) throw new Error('update_auto_setting_changed');
  const checked = await evaluate(`(() => {
    const e = document.querySelector('[data-update-panel] button[aria-label="Check for updates"]');
    if (!e || e.disabled || !e.getClientRects().length) return false;
    e.click(); return true;
  })()`);
  if (!checked) throw new Error('update_check_click_unproven');
  await pause(250);
  while (Date.now() < deadline) {
    state = await evaluate(panelExpression);
    if (state && state.status !== 'checking' && state.check_enabled) break;
    await pause(250);
  }
  if (!state || state.status === 'checking' || !state.check_enabled) throw new Error('update_check_did_not_settle');
  if (state.auto_update !== automatic) throw new Error('update_auto_setting_changed');
  if (['check-failed', 'update-failed', 'disabled', 'unknown'].includes(state.status)) throw new Error('update_check_failed');
  if (values.action === 'update-inspect') return { ok: true, badge_clicked: true, check_clicked: true, ...state };
  if (state.status !== 'available' || state.offered !== targetVersion) throw new Error('update_target_not_discoverable');
  const clicked = await evaluate(`(() => {
    const buttons = [...document.querySelectorAll('[data-update-panel] button')]
      .filter(e => e.textContent.trim() === 'Update' && !e.disabled && e.getClientRects().length);
    if (buttons.length !== 1) return false;
    buttons[0].click(); return true;
  })()`);
  if (!clicked) throw new Error('update_button_click_unproven');
  return { ok: true, badge_clicked: true, check_clicked: true, update_clicked: true, ...state };
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
  if (updateAction) {
    console.log(JSON.stringify(await updatePanel()));
  } else {
  // Navigate to Settings, then use the actual visible rail link. This proves
  // a real Workbench DOM click, not merely a debugger listener or direct invoke.
  await navigateTo(origin.origin + '/settings');
  let navigationClicked = false;
  const navigationDeadline = Date.now() + 20000;
  while (Date.now() < navigationDeadline) {
    try {
      navigationClicked = await evaluate(`(() => {
        const a = [...document.querySelectorAll('a[href="/settings/computer-use"]')]
          .find(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
        if (!a) return false;
        a.click(); return true;
      })()`);
    } catch { /* A navigation can replace the execution context. */ }
    if (navigationClicked) break;
    await pause(250);
  }
  if (!navigationClicked) throw new Error('settings_navigation_click_unproven');
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
  if (!state || (!state.unsupported && (!state.status || state.status === 'loading'))) {
    const onRoute = await evaluate('location.pathname === "/settings/computer-use"');
    throw new Error(onRoute ? 'settings_page_not_ready' : 'settings_navigation_not_current');
  }
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
  console.log(JSON.stringify({ ok: true, navigation_clicked: navigationClicked, ...state }));
  }
} catch (error) {
  const reason = typeof error.message === 'string' && /^[a-z][a-z0-9_]+$/.test(error.message) ? error.message : 'cdp_fixture_failed';
  console.log(JSON.stringify({ ok: false, reason }));
  process.exitCode = 1;
} finally {
  for (const request of pending.values()) clearTimeout(request.timer);
  socket?.close();
}
