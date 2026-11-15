// Inert boundary for executing the *generated* acceptance JavaScript. This is
// not a model, Controller, CUA driver or product acceptance substitute.
import fs from 'node:fs';
import http from 'node:http';
import inspector from 'node:inspector';

const [mode, work, fault = 'none'] = process.argv.slice(2);
if (mode === 'client') {
  if (process.platform !== 'win32' && process.env.CU_DRY_FAULT === 'aggregate_timeout') await new Promise((resolve) => setTimeout(resolve, 3300));
  const args = process.argv.slice(4);
  const script = args[args.indexOf('--code') + 1]?.slice(1);
  if (!script) process.exit(41);
  const port = fs.readFileSync(`${work}/port`, 'utf8');
  const response = await fetch(`http://127.0.0.1:${port}/`, {method: 'POST', body: fs.readFileSync(script, 'utf8')});
  const result = await response.json();
  if (result.exit_code) process.exitCode = result.exit_code;
  if (result.empty) { /* intentional empty stdout */ }
  else if (result.malformed) process.stdout.write('deliberately-not-json');
  else process.stdout.write(JSON.stringify(result.envelope));
  if (result.stderr) process.stderr.write('inert-fixture-stderr');
} else {
  const session = new inspector.Session(); session.connect();
  let typed = false;
  let attempts = 0;
  let activations = 0;
  let observations = 0;
  const fail = (stage) => { if (fault === stage) { const error = new Error('fixture-only'); error.code = 'injected_boundary_failure'; throw error; } };
  const window = {
    windowId: 42,
    observe: async () => {
      if (fault === 'observe_before' && observations >= 2 && attempts === 0) fail('observe_before');
      if (fault === 'type_after' && attempts > 0) { const e = new Error('fixture'); e.code = 'injected_after_failure'; throw e; }
      fail(typed ? 'observe_after' : 'observe'); observations++;
      return {app_name: 'fixture', window_bounds: {}, tree_markdown: fault === 'unusable' ? '' : (fault === 'unstable' ? String(observations) : (typed ? 'edited' : 'blank')), elements: []};
    },
    activate: async () => { activations++; fs.writeFileSync(`${work}/activations`, String(activations)); fail('activate'); return {effect: 'activated', status: 'ok', route: 'fixture', delivery: {mode: 'fixture'}}; },
    type: async (...args) => {
      if (args.length !== 1) throw new Error('fixture_disallows_delivery_override');
      attempts++; fs.writeFileSync(`${work}/attempts`, String(attempts));
      if (fault === 'type_after') { const e = new Error('fixture'); e.code = 'injected_type_failure'; throw e; }
      fail('type'); typed = true; fs.writeFileSync(`${work}/typed`, 'yes');
      return {effect: 'typed', status: 'ok', route: 'fixture', delivery: {mode: 'background'}};
    },
  };
  globalThis.computer = {app: async () => { fail('app'); return {launch: async () => { fail('launch'); return {app: {pid: 123, waitFor: async () => { fail('wait_for'); return window; }}}; }}; }};
  const post = (params) => new Promise((resolve, reject) => session.post('Runtime.evaluate', params, (e, r) => e ? reject(e) : resolve(r)));
  const server = http.createServer(async (request, response) => {
    let code = ''; for await (const chunk of request) code += chunk;
    const isType = code.includes('typeDiagnostics.type_attempt_count = 1');
    const label = isType ? 'type' : 'launch';
    const cliFault = fault === `${label}_cli_exit`;
    const malformed = fault === `${label}_cli_json`;
    const empty = fault === `${label}_cli_empty`;
    let envelope;
    if (fault === 'lost_binding' && isType) await post({expression: 'win = undefined', replMode: true});
    if (!cliFault && !malformed && !empty) {
      // The shipped kernel uses replMode and const-as-let too. This fixture
      // only transforms these generated snippets, not arbitrary user code.
      const answer = await post({expression: code.replace(/\bconst\b/g, 'let'), replMode: true, awaitPromise: true, returnByValue: true});
      envelope = answer.exceptionDetails ? {ok: false, kind: 'computer_js', error: {code: 'js_exception'}} : {ok: true, kind: 'computer_js', result: answer.result.value};
    }
    if (fault === `${label}_cli_null`) envelope = null;
    if (fault === `${label}_cli_kind`) envelope = {ok: true, kind: 'wrong'};
    if (fault === `${label}_cli_fields`) envelope = {ok: true, kind: 'computer_js', result: {}};
    if (cliFault) envelope = {ok: false, code: 'injected_cli_failure'};
    response.setHeader('Content-Type', 'application/json');
    response.end(JSON.stringify({exit_code: cliFault ? 37 : null, malformed, empty, stderr: fault === 'stderr_zero', envelope}));
  });
  server.listen(0, '127.0.0.1', () => fs.writeFileSync(`${work}/port`, String(server.address().port)));
}
