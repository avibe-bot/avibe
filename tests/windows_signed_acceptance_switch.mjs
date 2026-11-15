#!/usr/bin/env node
// Hermetic oracle for the real settingsSwitch() from cdp.mjs: no browser,
// native API, network or product claim. The fake page models the #3815 switch:
// a click makes it pending, and only a later status read moves it to the
// server's value. Slicing is only a loading seam; behaviour is executed.
// Run: node tests/windows_signed_acceptance_switch.mjs --source <cdp.mjs>
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { parseArgs } from 'node:util';

const { values } = parseArgs({ options: { source: { type: 'string' } } });
if (!values.source) throw new Error('--source is required');
const source = fs.readFileSync(values.source, 'utf8');
const start = source.indexOf('async function settingsSwitch(');
const end = source.indexOf('async function updatePanel()', start);
if (start < 0 || end < 0) throw new Error('Update the function-loading seam for this source layout.');
const functionSource = source.slice(start, end);
const STATE = '__state__';

async function exercise(target, scenario) {
  // Server opt-in and the page's view of it.
  const page = { enabled: !target, pending: false, failed: false, clicks: 0, readsAfterClick: 0 };
  const statusFor = (enabled) => (enabled ? 'ready' : 'off');
  const switchElement = {
    get disabled() { return page.pending || scenario === 'disabled'; },
    getClientRects: () => [{ width: 10, height: 10 }],
    click() {
      page.clicks++; page.pending = true;
      if (scenario === 'rejected') { page.pending = false; page.failed = true; }
    },
  };
  const pageContext = vm.createContext({
    document: { querySelector: (selector) => (selector === '[data-testid="computer-use-switch"]' ? switchElement : null) },
  });
  const view = () => {
    // A read after the click settles the change, unless the change never lands.
    if (page.pending && scenario !== 'never-confirms') {
      page.readsAfterClick++;
      if (page.readsAfterClick >= 2) { page.pending = false; page.enabled = target; }
    }
    return {
      status: statusFor(page.enabled),
      switch_state: page.pending ? 'pending' : page.enabled ? 'on' : 'off',
      switch_disabled: switchElement.disabled,
      switch_failed: page.failed,
      switch_tray_only: scenario === 'tray-only',
    };
  };
  let clock = 0;
  const fixture = vm.createContext({
    Date: { now: () => clock },
    pause: async (ms) => { clock += ms; },
    evaluate: async (expression) => (expression === STATE ? view() : vm.runInContext(expression, pageContext, { timeout: 1000 })),
  });
  const initial = view();
  let result, error;
  try {
    result = await vm.runInContext(`${functionSource}\nsettingsSwitch(${target}, ${JSON.stringify(initial)}, ${JSON.stringify(STATE)}, 60000)`, fixture, { timeout: 1000 });
  } catch (e) { error = e.message; }
  return { result, error, clicks: page.clicks, enabled: page.enabled };
}

const on = await exercise(true, 'normal');
assert.equal(on.error, undefined);
assert.equal(on.result.switch_state, 'on');
assert.equal(on.result.status, 'ready');
assert.equal(on.clicks, 1);

const off = await exercise(false, 'normal');
assert.equal(off.error, undefined);
assert.equal(off.result.switch_state, 'off');
assert.equal(off.result.status, 'off');
assert.equal(off.clicks, 1);

// A rejected change shows the failure note: never a pass.
assert.equal((await exercise(false, 'rejected')).error, 'settings_switch_change_failed');
// The page cannot turn it off (tray-only): refuse without clicking.
const trayOnly = await exercise(false, 'tray-only');
assert.equal(trayOnly.error, 'settings_switch_tray_only');
assert.equal(trayOnly.clicks, 0);
// A disabled switch is not clicked.
const disabled = await exercise(false, 'disabled');
assert.equal(disabled.error, 'settings_switch_not_on');
assert.equal(disabled.clicks, 0);
// A change that never reaches the authoritative state times out.
assert.equal((await exercise(false, 'never-confirms')).error, 'settings_disable_not_confirmed');
assert.equal((await exercise(true, 'never-confirms')).error, 'settings_enable_not_ready');
console.log('settings_switch_oracle_cases_passed');
