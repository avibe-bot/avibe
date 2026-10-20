// Persistent JavaScript kernel for ``vibe computer js``.
//
// The Controller owns this process: it starts it lazily for one Avibe Session,
// talks to it over the inherited socket named by AVIBE_COMPUTER_KERNEL_FD
// (stdio is /dev/null), and kills it on timeout, idle, reset, archive, or
// shutdown. Every desktop effect still goes through the Controller's
// /internal/computer-script routes with the exact-Turn token of the call that
// is executing; outside an executing call there is no token to use.
//
// Evaluation uses V8's REPL mode through the in-process inspector, the same
// semantics as the DevTools console: top-level await, and top-level `let`,
// `const`, `class`, and `function` bindings that persist across calls and may
// be redeclared. It needs no parser, no extra dependency, and no experimental
// Node flag; a module-based design would lose non-exported bindings.

import { randomUUID } from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import inspector from "node:inspector";
import net from "node:net";
import path from "node:path";
import { inspect } from "node:util";
import * as runtime from "./runtime.mjs";
import { avibe, computer } from "./index.mjs";

const CONSOLE_TAIL_MAX_BYTES = 16 * 1024;
const RESULT_TEXT_MAX_CHARS = 16 * 1024;
const STACK_MAX_CHARS = 4 * 1024;
const RETAINED_CALL_DIRECTORIES = 10;

const channel = new net.Socket({
  fd: Number(process.env.AVIBE_COMPUTER_KERNEL_FD),
  readable: true,
  writable: true,
});
const session = new inspector.Session();
const captureKey = `__avibeKernelCapture_${randomUUID().replaceAll("-", "")}`;
let captured;
let artifactRoot;
let callSequence = 0;
let current;

Object.defineProperty(globalThis, captureKey, {
  value: (value) => {
    captured = value;
  },
  enumerable: false,
  configurable: false,
  writable: false,
});
Object.defineProperty(globalThis, "computer", { value: computer, enumerable: true });
Object.defineProperty(globalThis, "avibe", { value: avibe, enumerable: true });

// Timers belong to the call that scheduled them. Once that call ends (it
// completed, timed out, or its Turn ended) its callbacks are dropped, so a
// cancelled continuation waiting on a timer never resumes to touch bindings.
function owned(callback, onDropped) {
  const owner = runtime.kernelCalls.getStore();
  if (typeof callback !== "function" || owner === undefined) {
    return callback;
  }
  return function ownedCallback(...args) {
    if (owner.active === true) {
      return Reflect.apply(callback, this, args);
    }
    onDropped?.();
    return undefined;
  };
}
for (const name of ["setTimeout", "setImmediate", "queueMicrotask"]) {
  const original = globalThis[name];
  globalThis[name] = function (callback, ...rest) {
    return Reflect.apply(original, this, [owned(callback), ...rest]);
  };
}
const originalSetInterval = globalThis.setInterval;
globalThis.setInterval = function (callback, ...rest) {
  let handle;
  handle = Reflect.apply(originalSetInterval, this, [
    owned(callback, () => clearInterval(handle)),
    ...rest,
  ]);
  return handle;
};

function send(message) {
  let line;
  try {
    line = JSON.stringify(message);
  } catch {
    line = undefined;
  }
  if (line === undefined || Buffer.byteLength(line) > runtime.PROTOCOL_FILE_MAX_BYTES) {
    const error = new Error("The call result exceeded the protocol size limit.");
    error.name = "ControllerError";
    error.code = "computer_script_protocol_error";
    error.params = { limit_bytes: runtime.PROTOCOL_FILE_MAX_BYTES };
    line = JSON.stringify({
      type: "result",
      id: message.id,
      ok: false,
      error: runtime.failureEnvelope(error),
      console: message.console ?? "",
      screenshots: message.screenshots ?? [],
    });
  }
  channel.write(`${line}\n`);
}

function post(method, params) {
  return new Promise((resolve, reject) => {
    session.post(method, params, (error, result) =>
      error ? reject(error) : resolve(result),
    );
  });
}

async function materialize(remote) {
  if (!remote || remote.type === "undefined") {
    return undefined;
  }
  if (remote.objectId) {
    captured = undefined;
    await post("Runtime.callFunctionOn", {
      objectId: remote.objectId,
      functionDeclaration: `function () { globalThis[${JSON.stringify(captureKey)}](this); }`,
      silent: true,
    });
    return captured;
  }
  const special = remote.unserializableValue;
  if (typeof special === "string") {
    return special.endsWith("n") ? BigInt(special.slice(0, -1)) : Number(special);
  }
  return remote.value;
}

function jsonResult(value) {
  if (value === undefined) {
    return { result: null };
  }
  try {
    const encoded = JSON.stringify(value);
    if (encoded !== undefined) {
      return { result: JSON.parse(encoded) };
    }
  } catch {
    // Cyclic or BigInt values are reported as inspected text below.
  }
  return {
    result: null,
    result_text: inspect(value, { depth: 4, breakLength: 120 }).slice(
      0,
      RESULT_TEXT_MAX_CHARS,
    ),
  };
}

function newCall(message) {
  callSequence += 1;
  const artifactDirectory = path.join(artifactRoot, `call-${callSequence}`);
  fs.mkdirSync(artifactDirectory, { recursive: true, mode: 0o700 });
  pruneCallDirectories();
  let consoleTail = Buffer.alloc(0);
  return {
    id: message.id,
    active: true,
    connectionToken: message.connection_token,
    agent: new http.Agent({ keepAlive: true, maxSockets: 1, maxFreeSockets: 1 }),
    artifactDirectory,
    screenshots: [],
    progress: runtime.newProgress(),
    appendConsole(encoded) {
      const combined = Buffer.concat([consoleTail, encoded]);
      consoleTail = combined.subarray(
        Math.max(0, combined.length - CONSOLE_TAIL_MAX_BYTES),
      );
    },
    consoleText() {
      return consoleTail.toString("utf8");
    },
  };
}

function pruneCallDirectories() {
  const directories = fs
    .readdirSync(artifactRoot)
    .filter((name) => /^call-\d+$/.test(name))
    .sort((a, b) => Number(a.slice(5)) - Number(b.slice(5)));
  for (const name of directories.slice(0, -RETAINED_CALL_DIRECTORIES)) {
    fs.rmSync(path.join(artifactRoot, name), { recursive: true, force: true });
  }
}

function settle(call, outcome) {
  if (current !== call || !call.active) {
    return;
  }
  call.active = false;
  current = undefined;
  // In-flight requests of an ended call are dropped with its agent, so a
  // later call never queues behind them.
  call.agent.destroy();
  send({
    type: "result",
    id: call.id,
    ...outcome,
    console: call.consoleText(),
    screenshots: call.screenshots,
  });
}

function failure(call, error, defaultCode) {
  return runtime.kernelCalls.run(call, () => {
    const envelope = runtime.failureEnvelope(error, { defaultCode });
    // A plain exception from the agent's own code carries no Controller
    // context, so its stack is the useful part.
    if (envelope.code === "js_exception" && typeof error?.stack === "string") {
      envelope.params = { ...envelope.params, stack: error.stack.slice(0, STACK_MAX_CHARS) };
    }
    return envelope;
  });
}

async function evaluate(message) {
  const call = newCall(message);
  current = call;
  const group = `call-${call.id}`;
  try {
    const response = await runtime.kernelCalls.run(call, () =>
      post("Runtime.evaluate", {
        expression: message.code,
        replMode: true,
        awaitPromise: true,
        silent: true,
        objectGroup: group,
      }),
    );
    if (response.exceptionDetails) {
      const thrown = await materialize(response.exceptionDetails.exception);
      settle(call, { ok: false, error: failure(call, thrown, "js_exception") });
    } else {
      settle(call, { ok: true, ...jsonResult(await materialize(response.result)) });
    }
  } catch (error) {
    settle(call, { ok: false, error: failure(call, error, "js_kernel_error") });
  } finally {
    post("Runtime.releaseObjectGroup", { objectGroup: group }).catch(() => {});
  }
}

function cancel(message) {
  const call = current;
  if (!call || call.id !== message.id) {
    return;
  }
  const error = new Error("The JavaScript call was cancelled.");
  error.name = "Cancelled";
  error.code = typeof message.code === "string" ? message.code : "js_timeout";
  error.ambiguous = true;
  settle(call, { ok: false, error: failure(call, error, error.code) });
}

function handle(message) {
  if (message.type === "init") {
    runtime.configureKernel({ endpoint: message.endpoint });
    artifactRoot = message.artifact_directory;
    session.connect();
    send({ type: "ready" });
  } else if (message.type === "eval" && current === undefined) {
    void evaluate(message);
  } else if (message.type === "cancel") {
    cancel(message);
  }
}

let buffered = "";
channel.setEncoding("utf8");
channel.on("data", (chunk) => {
  buffered += chunk;
  let newline = buffered.indexOf("\n");
  while (newline >= 0) {
    const line = buffered.slice(0, newline);
    buffered = buffered.slice(newline + 1);
    newline = buffered.indexOf("\n");
    try {
      handle(JSON.parse(line));
    } catch {
      process.exit(70);
    }
  }
});
// The Controller is the only owner; once its end of the socket closes, no
// call can ever be admitted again.
channel.on("end", () => process.exit(0));
channel.on("error", () => process.exit(0));
