#!/usr/bin/env node
// Hermetic acceptance-oracle regression: no browser, native APIs, network,
// service, home/profile reads or product acceptance claims. Execute the real
// updatePanel() and its DOM expressions against a small fake page. Slicing is
// only a loading seam to exclude the CLI/network entrypoint, not an assertion.
// Run: node tests/windows_signed_acceptance_oracle.mjs --source <cdp.mjs>
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { parseArgs } from 'node:util';

const { values } = parseArgs({ options: { source: { type: 'string' } } });
if (!values.source) throw new Error('--source is required');
const source = fs.readFileSync(values.source, 'utf8');
const start = source.indexOf('async function updatePanel()');
const end = source.indexOf('\ntry {\n  const response = await fetch', start);
if (start < 0 || end < 0) throw new Error('Update the function-loading seam for this source layout.');
const functionSource = source.slice(start, end);

async function exercise(scenario) {
  const isUpdate = ['offer-change', 'fresh-update'].includes(scenario);
  const state = {
    current: '3.2.7rc3', offered: isUpdate ? '3.2.7rc4' : null,
    status: isUpdate ? 'available' : 'current',
    checkedAt: '2026-11-15T06:00:00.000Z', auto: false, prereleases: true,
    badgeClicks: 0, checkClicks: 0, installed: [], offerChanged: false,
  };
  const location = { origin: 'http://127.0.0.1:12345', pathname: '/' };
  const visible = () => [{ width: 10, height: 10 }];
  function element(text, attrs = {}, click = () => {}) {
    return {
      get textContent() { return typeof text === 'function' ? text() : text; },
      get innerText() { return this.textContent; }, disabled: false,
      getAttribute(name) { const v = attrs[name]; return typeof v === 'function' ? v() : v ?? null; },
      getClientRects: visible, click,
    };
  }
  const currentValue = element(() => state.current);
  const label = element('Current version'); label.nextElementSibling = currentValue;
  const offered = element(() => state.offered);
  const timestamp = element(() => state.checkedAt, { datetime: () => state.checkedAt });
  Object.defineProperty(timestamp, 'dateTime', { get: () => state.checkedAt });
  const auto = element('', { 'aria-checked': () => String(state.auto) });
  const prereleases = element('', { 'aria-checked': () => String(state.prereleases) }, () => { state.prereleases = !state.prereleases; });
  const check = element('Check for updates', { 'aria-label': 'Check for updates' }, () => {
    state.checkClicks++;
    // No-op case deliberately has no transition, receipt, or failure banner.
    // For the race case, checking succeeds instantly with a fresh visible receipt.
    if (scenario !== 'stale-check') state.checkedAt = '2026-11-15T06:00:01.000Z';
  });
  const update = element('Update', {}, () => { state.installed.push(state.offered); });
  const badge = element(() => state.current, { 'aria-controls': 'update-popover' }, () => {
    state.badgeClicks++;
    // Natural handoff caused by the actual simulated badge click, not navigation.
    location.origin = 'http://tauri.localhost'; location.pathname = '/account/index.html';
  });
  const panel = element('', { 'data-update-status': () => state.status });
  const unsupported = (scope, selector) => { throw new Error(`Unsupported fixture selector (${scope}): ${selector}`); };
  panel.querySelector = (selector) => {
    if (selector === 'a[href*="/releases/"] span') return state.offered ? offered : null;
    if (selector === '[role="switch"][aria-label="Update automatically"]') return auto;
    if (selector === '[role="switch"][aria-label="Get pre-release updates"]') return prereleases;
    if (selector === 'button[aria-label="Check for updates"]') return check;
    if (selector === 'time' || selector === 'time[datetime]') return timestamp;
    if (selector === '[role="alert"]') return null;
    return unsupported('panel', selector);
  };
  panel.querySelectorAll = (selector) => {
    if (selector === 'span') return [label, currentValue];
    if (selector === 'button') return state.offered ? [check, update] : [check];
    if (selector === '[role="alert"]') return [];
    return unsupported('panel.all', selector);
  };
  const document = {
    querySelectorAll(selector) {
      if (selector === 'button[aria-controls]') return [badge];
      if (selector === '[data-update-panel]') return [panel];
      if (selector === '[data-update-panel] button') return panel.querySelectorAll('button');
      if (selector === '[role="alert"]') return [];
      return unsupported('document.all', selector);
    },
    querySelector(selector) {
      if (selector === '[data-update-panel]') return panel;
      if (selector.startsWith('[data-update-panel] ')) return panel.querySelector(selector.slice('[data-update-panel] '.length));
      if (selector === '[role="alert"]') return null;
      return unsupported('document', selector);
    },
  };
  const page = vm.createContext({ document, location, getComputedStyle: () => ({ visibility: 'visible' }) });
  let clock = 0;
  const fixture = vm.createContext({
    values: { action: isUpdate ? 'update-start' : 'update-inspect', 'current-version': '3.2.7rc3', 'target-version': '3.2.7rc4' },
    origin: new URL('http://127.0.0.1:12345'), Date: { now: () => clock },
    pause: async (ms) => { clock += ms; },
    navigateTo: async () => { location.origin = 'http://127.0.0.1:12345'; location.pathname = '/'; },
    evaluate: async (expression) => {
      const result = vm.runInContext(expression, page, { timeout: 1000 });
      // Publish a different offer after the caller has sampled rc4, before its
      // next CDP evaluation. The next expression sees the REAL changed fake DOM.
      // An atomic version check+click therefore refuses instead of installing rc5.
      if (scenario === 'offer-change' && state.checkClicks && !state.offerChanged
          && result?.status === 'available' && result?.offered === '3.2.7rc4') {
        state.offered = '3.2.7rc5'; state.offerChanged = true;
      }
      return result;
    },
  });
  let result, error;
  try { result = await vm.runInContext(`${functionSource}\nupdatePanel()`, fixture, { timeout: 1000 }); }
  catch (e) { error = e.message; }
  return { scenario, result, error, badgeClicks: state.badgeClicks, checkClicks: state.checkClicks, installed: state.installed, offerChanged: state.offerChanged };
}

const stale = await exercise('stale-check');
const race = await exercise('offer-change');
const fresh = await exercise('fresh-check');
const update = await exercise('fresh-update');
console.log(JSON.stringify({ stale, race, fresh, update }));
// Unknown fake-DOM behavior must fail, not masquerade as a fixed oracle.
assert.equal(stale.error, 'update_fresh_check_receipt_unproven');
assert.equal(stale.result, undefined);
assert.equal(stale.checkClicks, 1);
assert.equal(race.error, 'update_offer_changed_before_click');
assert.equal(race.offerChanged, true);
assert.deepEqual(race.installed, []);
assert.equal(fresh.error, undefined);
assert.equal(fresh.result?.ok, true);
assert.equal(fresh.result?.fresh_check, true);
assert.equal(fresh.checkClicks, 1);
assert.deepEqual(fresh.installed, []);
assert.equal(update.error, undefined);
assert.equal(update.result?.ok, true);
assert.equal(update.result?.offered, '3.2.7rc4');
assert.equal(update.result?.update_clicked, true);
assert.deepEqual(update.installed, ['3.2.7rc4']);
// The value-free per-step panel record must describe what the fake panel
// actually showed: opened (before Check), after the fresh Check, and at the
// atomic Update click. It never contains anything beyond versions/flags.
assert.equal(update.result?.steps?.opened?.current, '3.2.7rc3');
assert.equal(update.result?.steps?.opened?.checked_at, '2026-11-15T06:00:00.000Z');
assert.equal(update.result?.steps?.after_check?.status, 'available');
assert.equal(update.result?.steps?.after_check?.offered, '3.2.7rc4');
assert.equal(update.result?.steps?.after_check?.checked_at, '2026-11-15T06:00:01.000Z');
assert.equal(update.result?.steps?.at_update_click?.offered, '3.2.7rc4');
assert.equal(update.result?.steps?.prerelease_switch_clicked, false);
assert.deepEqual(Object.keys(update.result?.steps?.opened ?? {}).sort(),
  ['auto_update', 'check_enabled', 'checked_at', 'current', 'offered', 'panel_view', 'prereleases', 'status']);
assert.equal(fresh.result?.steps?.after_check?.status, 'current');
