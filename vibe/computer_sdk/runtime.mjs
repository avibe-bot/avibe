import fs from "node:fs";
import http from "node:http";
import path from "node:path";

const MAX_RESPONSE_BYTES = 128 * 1024 * 1024;

let state;
let agent;
let currentStep = "script";
let lastStep = "script";
let lastScreenshotPath;
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
    !Number.isInteger(config.timeout_ms) ||
    config.timeout_ms <= 0 ||
    typeof config.artifact_directory !== "string"
  ) {
    throw new Error("invalid computer SDK runtime configuration");
  }
  validateEndpoint(config.endpoint);
  const wallClockRemaining =
    Number.isFinite(config.deadline_unix_ms) && config.deadline_unix_ms > 0
      ? config.deadline_unix_ms - Date.now()
      : config.timeout_ms;
  state = {
    endpoint: config.endpoint,
    connectionToken: config.connection_token,
    deadline: performance.now() + Math.max(1, wallClockRemaining),
    artifactDirectory: config.artifact_directory,
  };
  agent = new http.Agent({
    keepAlive: true,
    maxSockets: 1,
    maxFreeSockets: 1,
    scheduling: "fifo",
  });
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

export function remainingTimeMs() {
  requireConfigured();
  return Math.max(0, state.deadline - performance.now());
}

export async function sleepWithinDeadline(milliseconds) {
  const remaining = remainingTimeMs();
  if (remaining <= 0) {
    throw new ControllerFault("script_timeout", "The script deadline expired.");
  }
  await new Promise((resolve) =>
    setTimeout(resolve, Math.min(Math.max(0, milliseconds), remaining)),
  );
}

export async function runStep(label, operation) {
  currentStep = label;
  lastStep = label;
  try {
    return await operation();
  } catch (error) {
    if (error && typeof error === "object" && !error.step) {
      error.step = label;
    }
    throw error;
  } finally {
    currentStep = "script";
  }
}

export async function callTool(tool, argumentsValue, options = {}) {
  const body = await postJson(
    "/internal/computer-script/request",
    {
      connection_token: state.connectionToken,
      tool,
      arguments: argumentsValue,
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
    lastScreenshotPath = structured.screenshot_file_path;
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
    lastScreenshotPath = target;
  } catch {
    // A screenshot artifact is diagnostic only; the structured observation
    // remains authoritative when the local file cannot be written.
  }
}

async function postJson(requestPath, payload, requestOptions = {}) {
  requireConfigured();
  const scriptRemaining = remainingTimeMs();
  const localTimeout =
    Number.isFinite(requestOptions.timeoutMs) && requestOptions.timeoutMs >= 0
      ? requestOptions.timeoutMs
      : undefined;
  const localDeadlineWins =
    localTimeout !== undefined && localTimeout < scriptRemaining;
  const remaining = Math.min(scriptRemaining, localTimeout ?? scriptRemaining);
  if (remaining <= 0) {
    throw requestTimeoutFault(localDeadlineWins, false);
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
    let requestFinished = false;
    let timer;
    const settle = (callback, value) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      callback(value);
    };
    const request = http.request(transportOptions, (response) => {
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
              { ambiguous: requestFinished },
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
              { ambiguous: requestFinished },
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
              { ambiguous: requestFinished },
            ),
          );
          return;
        }
        settle(resolve, body);
      });
      response.on("error", (error) => settle(reject, error));
    });
    timer = setTimeout(() => {
      request.destroy(
        requestTimeoutFault(localDeadlineWins, requestFinished),
      );
    }, remaining);
    request.on("finish", () => {
      requestFinished = true;
    });
    request.on("error", (error) => {
      if (error instanceof ControllerFault) {
        settle(reject, error);
        return;
      }
      settle(
        reject,
        new ControllerFault(
          "controller_unavailable",
          `The Controller connection failed: ${error.message}`,
          {},
          { ambiguous: requestFinished },
        ),
      );
    });
    request.end(encoded);
  });
}

function requestTimeoutFault(localDeadlineWins, ambiguous) {
  if (localDeadlineWins) {
    return new ControllerFault(
      "timeout",
      "The Computer Use request exceeded the operation timeout.",
      {},
      { ambiguous },
    );
  }
  return new ControllerFault(
    "script_timeout",
    "The script deadline expired.",
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
      { timeoutMs: Math.min(500, remainingTimeMs()) },
    );
  } finally {
    agent.destroy();
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
      typeof error.screenshotPath === "string"
        ? error.screenshotPath
        : lastScreenshotPath,
  };
}

function requireConfigured() {
  if (!state || !agent) {
    throw new Error("computer SDK runtime is not configured");
  }
}
