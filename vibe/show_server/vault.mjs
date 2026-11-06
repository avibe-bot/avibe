// Server-only: never import this module from a Show browser entry/barrel.
// The Python Show owner refreshes this private transport snapshot before each
// admitted API request. No CLI subprocess, database, secret lookup or replay.
import { constants } from 'node:fs';
import { lstat, open } from 'node:fs/promises';
import http from 'node:http';

const CALLER_ENV_NAMES = [
  'AVIBE_SESSION_ID', 'AVIBE_RUN_ID', 'AVIBE_CALLER_SOURCE', 'AVIBE_CALLER_BACKEND',
  'AVIBE_NATIVE_SESSION_ID', 'AVIBE_CALLER_PLATFORM', 'AVIBE_CALLER_USER_ID',
  'AVIBE_CALLER_CHANNEL_ID', 'AVIBE_CALLER_SESSION_KEY', 'AVIBE_CALLER_MESSAGE_ID',
  'AVIBE_CALLER_WORKSPACE_ID', 'AVIBE_CALLER_REMOTE', 'AVIBE_CALLER_RESOURCE_CONTEXT',
];

function failure(code, execution = 'not_executed') {
  const unknown = execution === 'execution_unknown';
  return {
    version: 1, execution, exit_code: 1,
    payload: { schema_version: 1, ok: false, kind: 'error', code, execution,
      error: unknown ? 'Vault result could not be confirmed; do not replay the request' : 'Vault request was not executed',
      hint: unknown ? 'Inspect the upstream state before submitting new work.' : 'Start or update Avibe explicitly before trying again.' },
  };
}

function privateStat(info, directory = false) {
  return process.platform === 'win32' || (
    info.uid === process.getuid() && (info.mode & 0o777) === (directory ? 0o700 : 0o600)
  );
}

async function endpoint() {
  const path = process.env.AVIBE_SHOW_CONTROL_SNAPSHOT;
  if (!path) throw new Error('unavailable');
  let value;
  // Atomic publication can replace a valid file between lstat and open.
  // Retry discovery only, never an operation that has already been submitted.
  for (let attempt = 0; attempt < 3; attempt++) {
    const before = await lstat(path);
    if (!before.isFile() || !privateStat(before) || before.size > 65536) throw new Error('unavailable');
    const file = await open(path, constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
    try {
      const info = await file.stat();
      if (!info.isFile() || !privateStat(info) || info.size > 65536) throw new Error('unavailable');
      if (info.ino !== before.ino || info.dev !== before.dev) continue;
      const buffer = Buffer.alloc(65537);
      const { bytesRead } = await file.read(buffer, 0, buffer.length, 0);
      if (bytesRead > 65536) throw new Error('unavailable');
      value = JSON.parse(buffer.subarray(0, bytesRead).toString('utf8'));
      break;
    } finally {
      await file.close();
    }
  }
  if (value?.schema_version !== 1 || !Object.hasOwn(value, 'endpoint')) throw new Error('unavailable');
  const target = value.endpoint;
  if (target?.transport === 'unix' && typeof target.socket_path === 'string') {
    const info = await lstat(target.socket_path);
    if (!info.isSocket() || info.uid !== process.getuid() || ![0o600, 0o700].includes(info.mode & 0o777)) {
      throw new Error('unavailable');
    }
    return target;
  }
  if (target?.transport === 'tcp' && target.host === '127.0.0.1'
      && Number.isInteger(target.port) && target.port > 0 && target.port <= 65535
      && typeof target.headers?.Authorization === 'string'
      && /^Bearer [A-Za-z0-9_-]+$/.test(target.headers.Authorization)
      && typeof target.instance_id === 'string' && /^[A-Za-z0-9_-]+$/.test(target.instance_id)
      && target.instance_header === 'X-Avibe-Control-Instance') {
    return target;
  }
  throw new Error('unavailable');
}

function validate(value, operation) {
  if (value?.version !== 1 || !['completed', 'not_executed', 'execution_unknown'].includes(value.execution)
      || ![0, 1].includes(value.exit_code) || typeof value.payload?.ok !== 'boolean'
      || (value.exit_code === 0) !== value.payload.ok) throw new Error('ambiguous');
  const payload = value.payload;
  if (payload.kind === 'error' && payload.ok === false) {
    if (value.approval !== undefined) {
      const approval = value.approval;
      if (operation !== 'vault.fetch' || value.execution !== 'not_executed' || payload.code !== 'approval_required'
          || typeof approval?.auth !== 'string' || !approval.auth
          || typeof approval.request_id !== 'string' || !approval.request_id
          || !Number.isFinite(approval.timeout) || approval.timeout <= 0) throw new Error('ambiguous');
    }
  } else if (operation === 'vault.fetch' && payload.kind === 'vault_fetch') {
    if (value.execution !== 'completed' || typeof payload.body !== 'string' || !Number.isInteger(payload.status)
        || payload.ok !== (payload.status >= 200 && payload.status <= 299) || value.approval !== undefined) throw new Error('ambiguous');
  } else if (operation === 'vault.fetch.await' && payload.kind === 'vault_fetch_approved') {
    if (!payload.ok || value.execution !== 'not_executed' || value.approval !== undefined) throw new Error('ambiguous');
  } else throw new Error('ambiguous');
  return value;
}

function submit(target, operation, args, callerEnv, cwd, timeout, signal) {
  if (signal?.aborted) return Promise.resolve(failure('request_cancelled'));
  let content;
  try {
    content = Buffer.from(JSON.stringify({ version: 1, operation, arguments: args, caller_env: callerEnv, cwd,
      deadline: Date.now() / 1000 + 30 }));
  } catch {
    return Promise.resolve(failure('invalid_control_request'));
  }
  return new Promise((resolve) => {
    let connected = false;
    let settled = false;
    let timer;
    let req;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      resolve(value);
    };
    const abort = () => {
      finish(failure(connected ? 'execution_unknown' : 'request_cancelled', connected ? 'execution_unknown' : 'not_executed'));
      req?.destroy();
    };
    const options = {
      method: 'POST', path: '/internal/control', agent: false,
      headers: { ...(target.headers || {}), 'Content-Type': 'application/json', 'Content-Length': content.length },
      ...(target.transport === 'unix' ? { socketPath: target.socket_path } : { host: target.host, port: target.port }),
    };
    try {
      req = http.request(options, (res) => {
        const chunks = [];
        res.on('data', (chunk) => chunks.push(chunk));
        res.on('error', () => finish(failure('execution_unknown', 'execution_unknown')));
        res.on('end', () => {
          if ([401, 404].includes(res.statusCode)) return finish(failure('service_unavailable'));
          try {
            if (target.transport === 'tcp' && res.headers[target.instance_header.toLowerCase()] !== target.instance_id) {
              throw new Error('ambiguous');
            }
            finish(validate(JSON.parse(Buffer.concat(chunks).toString('utf8')), operation));
          } catch {
            finish(failure('execution_unknown', 'execution_unknown'));
          }
        });
        res.on('aborted', () => finish(failure('execution_unknown', 'execution_unknown')));
      });
      req.on('socket', (socket) => socket.once('connect', () => { connected = true; }));
      req.on('error', (error) => {
        const refused = !connected && ['ECONNREFUSED', 'ENOENT', 'EACCES'].includes(error.code);
        finish(failure(refused ? 'service_unavailable' : 'execution_unknown', refused ? 'not_executed' : 'execution_unknown'));
      });
      signal?.addEventListener('abort', abort, { once: true });
      timer = setTimeout(abort, timeout * 1000);
      if (signal?.aborted) abort();
      else req.end(content);
    } catch {
      finish(failure('service_unavailable'));
      req?.destroy();
    }
  });
}

/** Fetch through avault custody. Upstream non-2xx remains a body/status result.
 * Caller context is the explicit/inherited CLI environment, NOT HTTP identity.
 */
export async function vaultFetch(options, context = {}) {
  let target;
  try { target = await endpoint(); }
  catch { return failure('service_unavailable'); }
  const env = context.callerEnv ?? process.env;
  const callerEnv = Object.fromEntries(CALLER_ENV_NAMES.filter((key) => typeof env[key] === 'string').map((key) => [key, env[key]]));
  let cwd;
  try { cwd = context.cwd ?? process.cwd(); } catch { cwd = ''; }
  let headers;
  if (Array.isArray(options.headers)) headers = options.headers;
  else if (options.headers) headers = Object.entries(options.headers).map(([key, value]) => `${key}: ${value}`);
  const args = {
    auth: options.auth, url: options.url, method: options.method ?? 'GET', header: headers ?? null,
    body_b64: options.body == null ? null : Buffer.from(options.body, 'utf8').toString('base64'),
    caller_pid: process.pid, session_id: null, approval_wait: options.approvalWaitSeconds ?? null,
    no_approval_wait: options.noApprovalWait ?? false,
  };
  let response = await submit(target, 'vault.fetch', args, callerEnv, cwd, 125, context.signal);
  const approval = response.approval;
  if (approval) {
    response = await submit(target, 'vault.fetch.await', {
      request_id: approval.request_id, timeout: approval.timeout, session_id: null,
    }, callerEnv, cwd, 30 + approval.timeout + 35, context.signal);
    if (response.exit_code === 0 && response.payload.kind === 'vault_fetch_approved') {
      response = await submit(target, 'vault.fetch', {
        ...args, auth: approval.auth, approval_wait: null, no_approval_wait: true,
      }, callerEnv, cwd, 125, context.signal);
    }
  }
  return response;
}
