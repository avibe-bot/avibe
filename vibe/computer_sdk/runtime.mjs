import fs from "node:fs";
import http from "node:http";
import path from "node:path";

const MAX_RESPONSE_BYTES = 128 * 1024 * 1024;

const failureEnvelopeContract = JSON.parse(
  fs.readFileSync(new URL("./failure-envelope.json", import.meta.url), "utf8"),
);
if (
  !Array.isArray(failureEnvelopeContract.fields) ||
  failureEnvelopeContract.fields.some(
    (field) => typeof field !== "string" || !field,
  ) ||
  new Set(failureEnvelopeContract.fields).size !==
    failureEnvelopeContract.fields.length
) {
  throw new Error("invalid failure envelope contract");
}
export const FAILURE_ENVELOPE_FIELDS = Object.freeze(
  failureEnvelopeContract.fields,
);

let state;
let agent;
let currentStep = "script";
let lastStep = "script";
let lastScreenshotPath;
let lastOwnedScreenshotPath;
let screenshotSequence = 0;

export class ControllerFault extends Error {
  constructor(code, message, params = {}, options = {}) {
    super(message);
    this.name = "ControllerFault";
    this.code = code;
    this.params = params;
    this.ambiguous = options.ambiguous === true;
  }
}

export function configureRuntime(config) {
  if (state !== undefined) {
    throw new Error("computer SDK runtime is already configured");
  }
  if (
    !config ||
    typeof config.connection_token !== "string" ||
    !config.connection_token ||
    typeof config.artifact_directory !== "string" ||
    typeof config.runtime_state_path !== "string"
  ) {
    throw new Error("invalid computer SDK runtime configuration");
  }
  validateEndpoint(config.endpoint);
  state = {
    endpoint: config.endpoint,
    connectionToken: config.connection_token,
    artifactDirectory: config.artifact_directory,
    runtimeStatePath: config.runtime_state_path,
    inFlightRequests: new Set(),
  };
  agent = new http.Agent({
    keepAlive: true,
    maxSockets: 1,
    maxFreeSockets: 1,
    scheduling: "fifo",
  });
  persistRuntimeState();
}

function validateEndpoint(endpoint) {
  if (!endpoint || typeof endpoint !== "object") {
    throw new Error("invalid Controller endpoint");
  }
  if (
    endpoint.transport === "unix" &&
    typeof endpoint.socket_path === "string" &&
    endpoint.socket_path
  ) {
    return;
  }
  if (
    endpoint.transport === "tcp" &&
    typeof endpoint.host === "string" &&
    Number.isInteger(endpoint.port) &&
    endpoint.port > 0 &&
    endpoint.headers &&
    typeof endpoint.headers === "object" &&
    typeof endpoint.instance_id === "string" &&
    typeof endpoint.instance_header === "string"
  ) {
    return;
  }
  throw new Error("invalid Controller endpoint");
}

export async function sleepWithinDeadline(milliseconds) {
  await new Promise((resolve) =>
    setTimeout(resolve, Math.max(0, milliseconds)),
  );
}

export async function runStep(label, operation) {
  currentStep = label;
  lastStep = label;
  persistRuntimeState();
  try {
    return await operation();
  } catch (error) {
    if (error && typeof error === "object" && !error.step) {
      error.step = label;
    }
    throw error;
  } finally {
    currentStep = "script";
    persistRuntimeState();
  }
}

export async function callTool(tool, argumentsValue, options = {}) {
  const body = await postJson(
    "/internal/computer-script/request",
    {
      connection_token: state.connectionToken,
      tool,
      arguments: argumentsValue,
      descriptor: options.descriptor ?? null,
    },
    options,
  );
  const result = body?.result;
  if (result && typeof result === "object") {
    recordScreenshot(result);
    if (result.isError === true) {
      const structured = result.structuredContent;
      const code =
        structured && typeof structured.code === "string"
          ? structured.code
          : "computer_use_error";
      const params =
        structured && structured.params && typeof structured.params === "object"
          ? structured.params
          : {};
      throw new ControllerFault(code, controllerMessage(code, structured), params);
    }
    return result;
  }
  if (body?.ok === false) {
    const code =
      typeof body.error === "string" ? body.error : "computer_script_request_failed";
    throw new ControllerFault(code, controllerMessage(code, body), body.params);
  }
  throw new ControllerFault(
    "invalid_computer_use_response",
    "The Controller returned an invalid Computer Use response.",
  );
}

function controllerMessage(code, payload) {
  if (payload && typeof payload.detail === "string" && payload.detail) {
    return payload.detail;
  }
  return `Controller error: ${code}`;
}

function recordScreenshot(result) {
  const structured = result.structuredContent;
  if (
    structured &&
    typeof structured === "object" &&
    typeof structured.screenshot_file_path === "string" &&
    structured.screenshot_file_path
  ) {
    replaceDiagnosticScreenshot(structured.screenshot_file_path);
    return;
  }
  const image = Array.isArray(result.content)
    ? result.content.find(
        (item) =>
          item &&
          item.type === "image" &&
          typeof item.data === "string" &&
          (!item.mimeType || item.mimeType === "image/png"),
      )
    : undefined;
  if (!image) {
    return;
  }
  try {
    screenshotSequence += 1;
    const target = path.join(
      state.artifactDirectory,
      `observation-${screenshotSequence}.png`,
    );
    fs.writeFileSync(target, Buffer.from(image.data, "base64"), { mode: 0o600 });
    replaceDiagnosticScreenshot(target, { owned: true });
  } catch {
    // A screenshot artifact is diagnostic only; the structured observation
    // remains authoritative when the local file cannot be written.
  }
}

function replaceDiagnosticScreenshot(target, options = {}) {
  const previousOwned = lastOwnedScreenshotPath;
  lastScreenshotPath = target;
  lastOwnedScreenshotPath = options.owned === true ? target : undefined;
  persistRuntimeState();
  if (previousOwned && previousOwned !== lastOwnedScreenshotPath) {
    try {
      fs.unlinkSync(previousOwned);
    } catch {
      // The current screenshot remains valid even if an old diagnostic file
      // was already removed or cannot be cleaned up.
    }
  }
}

async function postJson(requestPath, payload, requestOptions = {}) {
  requireConfigured();
  const localTimeout =
    Number.isFinite(requestOptions.timeoutMs) && requestOptions.timeoutMs >= 0
      ? requestOptions.timeoutMs
      : undefined;
  if (localTimeout !== undefined && localTimeout <= 0) {
    throw requestTimeoutFault();
  }
  const encoded = Buffer.from(JSON.stringify(payload));
  const endpoint = state.endpoint;
  const headers = {
    ...(endpoint.transport === "tcp" ? endpoint.headers : {}),
    "Content-Type": "application/json",
    "Content-Length": String(encoded.length),
    Connection: "keep-alive",
  };
  const transportOptions =
    endpoint.transport === "unix"
      ? {
          socketPath: endpoint.socket_path,
          path: requestPath,
          method: "POST",
          headers,
          agent,
        }
      : {
          hostname: endpoint.host,
          port: endpoint.port,
          path: requestPath,
          method: "POST",
          headers,
          agent,
        };

  return new Promise((resolve, reject) => {
    let settled = false;
    let timer;
    const requestState = { sent: false };
    state.inFlightRequests.add(requestState);
    const settle = (callback, value) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      state.inFlightRequests.delete(requestState);
      callback(value);
    };
    let request;
    try {
      request = http.request(transportOptions, (response) => {
        if (endpoint.transport === "tcp") {
          const actual = response.headers[endpoint.instance_header.toLowerCase()];
          if (actual !== endpoint.instance_id) {
            response.resume();
            settle(
              reject,
              new ControllerFault(
                "controller_unavailable",
                "The Controller response came from a stale instance.",
                {},
                { ambiguous: hasAmbiguousInFlightRequest() },
              ),
            );
            return;
          }
        }
        const chunks = [];
        let size = 0;
        response.on("data", (chunk) => {
          size += chunk.length;
          if (size > MAX_RESPONSE_BYTES) {
            response.destroy(
              new ControllerFault(
                "invalid_computer_use_response",
                "The Controller response exceeded the supported size.",
                {},
                { ambiguous: hasAmbiguousInFlightRequest() },
              ),
            );
            return;
          }
          chunks.push(chunk);
        });
        response.on("end", () => {
          let body;
          try {
            body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
          } catch {
            settle(
              reject,
              new ControllerFault(
                "invalid_computer_use_response",
                "The Controller returned invalid JSON.",
                {},
                { ambiguous: hasAmbiguousInFlightRequest() },
              ),
            );
            return;
          }
          settle(resolve, body);
        });
        response.on("error", (error) =>
          settle(reject, controllerTransportFault(error)),
        );
      });
    } catch (error) {
      settle(reject, controllerTransportFault(error));
      return;
    }
    if (localTimeout !== undefined) {
      timer = setTimeout(() => {
        request.destroy(requestTimeoutFault());
      }, localTimeout);
    }
    request.on("finish", () => {
      requestState.sent = true;
    });
    request.on("error", (error) => {
      settle(reject, controllerTransportFault(error));
    });
    request.end(encoded);
  });
}

function hasAmbiguousInFlightRequest() {
  return (
    state?.inFlightRequests instanceof Set &&
    [...state.inFlightRequests].some((requestState) => requestState.sent === true)
  );
}

function controllerTransportFault(error) {
  if (error instanceof ControllerFault) {
    return error;
  }
  const message =
    error && typeof error === "object" && typeof error.message === "string"
      ? error.message
      : String(error);
  return new ControllerFault(
    "controller_unavailable",
    `The Controller connection failed: ${message}`,
    {},
    { ambiguous: hasAmbiguousInFlightRequest() },
  );
}

function requestTimeoutFault() {
  const ambiguous = hasAmbiguousInFlightRequest();
  return new ControllerFault(
    "timeout",
    "The Computer Use request exceeded the operation timeout.",
    {},
    { ambiguous },
  );
}

export async function closeScriptConnection() {
  if (!state || !agent) {
    return;
  }
  try {
    await postJson(
      "/internal/computer-script/close",
      {
        connection_token: state.connectionToken,
      },
      { timeoutMs: 500 },
    );
  } finally {
    agent.destroy();
  }
}

function persistRuntimeState() {
  if (!state?.runtimeStatePath) {
    return;
  }
  const temporaryPath = `${state.runtimeStatePath}.${process.pid}.tmp`;
  try {
    fs.writeFileSync(
      temporaryPath,
      JSON.stringify({
        step: currentStep || lastStep || "script",
        screenshot_path: lastScreenshotPath ?? null,
      }),
      { mode: 0o600 },
    );
    fs.renameSync(temporaryPath, state.runtimeStatePath);
  } catch {
    try {
      fs.unlinkSync(temporaryPath);
    } catch {
      // Runtime state is diagnostic; script execution remains authoritative.
    }
  }
}

export function failureContext(error) {
  return {
    step:
      error && typeof error === "object" && typeof error.step === "string"
        ? error.step
        : currentStep || lastStep || "script",
    screenshot_path:
      error &&
      typeof error === "object" &&
      (typeof error.screenshot_path === "string" ||
        typeof error.screenshotPath === "string")
        ? error.screenshot_path || error.screenshotPath
        : lastScreenshotPath,
  };
}

export function failureEnvelope(error, options = {}) {
  const context = failureContext(error);
  const params =
    error &&
    typeof error === "object" &&
    error.params &&
    typeof error.params === "object" &&
    !Array.isArray(error.params)
      ? error.params
      : {};
  const values = {
    name:
      error && typeof error === "object" && typeof error.name === "string"
        ? error.name
        : "Error",
    code:
      error && typeof error === "object" && typeof error.code === "string"
        ? error.code
        : options.defaultCode || "computer_script_failed",
    message:
      error && typeof error === "object" && typeof error.message === "string"
        ? error.message
        : String(error),
    params,
    step: context.step,
    screenshot_path: context.screenshot_path ?? null,
    ambiguous:
      error && typeof error === "object" && error.ambiguous === true,
  };
  for (const field of FAILURE_ENVELOPE_FIELDS) {
    if (!Object.hasOwn(values, field)) {
      throw new Error(`failure envelope field is not implemented: ${field}`);
    }
  }
  return Object.fromEntries(
    FAILURE_ENVELOPE_FIELDS.map((field) => [field, values[field]]),
  );
}

function requireConfigured() {
  if (!state || !agent) {
    throw new Error("computer SDK runtime is not configured");
  }
}
