// Real SDK calls; the HTTP fixture transports requests, never manufactures errors.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const sdkRoot = path.join(config.repo, 'vibe', 'computer_sdk');
const runtime = await import(pathToFileURL(path.join(sdkRoot, 'runtime.mjs')));
const { computer, ControllerError } = await import(pathToFileURL(path.join(sdkRoot, 'index.mjs')));
runtime.configureRuntime(config.runtime);

async function attach(target) {
  const app = await computer.app({ nameContains: target.name });
  assert.equal(app.pid, target.pid, 'lookup must select the retained fixture PID');
  const win = await app.window({ windowId: target.hwnd });
  assert.equal(win.windowId, target.hwnd);
  await win.observe({ include_screenshot: false });
  return win;
}

try {
  const elevated = await attach(config.elevated);
  let refusal;
  try {
    await elevated.type('must-not-be-inserted');
  } catch (error) {
    assert.ok(error instanceof ControllerError, 'must throw the real public SDK error');
    refusal = { name: error.name, code: error.code, params: error.params, message: error.message };
  }
  assert.ok(refusal, 'elevated input unexpectedly succeeded');
  assert.equal(refusal.code, 'protected_window');
  assert.deepEqual(refusal.params, { reason: 'administrator' });
  assert.equal(refusal.message, config.expected_message);

  const control = await attach(config.control);
  await control.activate();
  await control.type(config.control_text);
  fs.writeFileSync(config.sdk_result, JSON.stringify({
    elevated_observed: true, refusal, control_observed: true, control_type_returned: true,
  }, null, 2));
} finally {
  await runtime.closeScriptConnection();
}
