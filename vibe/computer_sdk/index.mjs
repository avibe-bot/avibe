import {
  ControllerFault,
  callTool,
  callScriptInteraction,
  controllerFailureAmbiguous,
  runStep,
  sleepWithinDeadline,
} from "./runtime.mjs";

const DEFAULT_WAIT_MS = 5000;
const POLL_INTERVAL_MS = 100;
const EFFECT_STATUSES = new Set([
  "confirmed",
  "partial",
  "unverifiable",
  "suspected_noop",
  "refused",
]);
const STALE_ELEMENT_CODES = new Set([
  "observe_first",
  "element_not_found",
  "element_token_invalid",
  "element_token_stale",
  "stale_element_token",
  "snapshot_not_found",
  "snapshot_stale",
]);
const NOT_FOUND_CODES = new Set([
  "application_not_found",
  "element_not_found",
  "snapshot_not_found",
  "window_id_not_found",
  "window_not_found",
]);
const BASE_SELECTOR_FIELDS = new Set([
  "role",
  "label",
  "labelContains",
  "value",
  "index",
]);
const APP_SELECTOR_FIELDS = new Set([
  ...BASE_SELECTOR_FIELDS,
  "bundleId",
  "name",
  "nameContains",
]);
const WINDOW_SELECTOR_FIELDS = new Set([
  ...BASE_SELECTOR_FIELDS,
  "title",
  "titleContains",
  "windowId",
  "onScreen",
]);
const ELEMENT_SELECTOR_FIELDS = new Set([
  ...BASE_SELECTOR_FIELDS,
  "within",
]);
const windowState = new WeakMap();

function invalidHandleResponse(message) {
  throw new ControllerError(message, {
    code: "invalid_computer_use_response",
    ambiguous: controllerFailureAmbiguous(true),
  });
}

function appDescriptor(record) {
  const bundleId =
    typeof record.bundle_id === "string" && record.bundle_id
      ? record.bundle_id
      : null;
  const name =
    bundleId === null && typeof record.name === "string" && record.name
      ? record.name
      : null;
  if (bundleId === null && name === null) {
    invalidHandleResponse(
      "The Controller returned an application without a stable identity.",
    );
  }
  return Object.freeze({
    kind: "app",
    pid: Number.isInteger(record.pid) && record.pid > 0 ? record.pid : null,
    bundle_id: bundleId,
    name,
  });
}

function windowDescriptor(app, record) {
  const descriptor = app._descriptor;
  if (
    !descriptor ||
    !Number.isInteger(descriptor.pid) ||
    descriptor.pid <= 0 ||
    record.pid !== descriptor.pid ||
    !Number.isInteger(record.window_id) ||
    record.window_id < 0
  ) {
    invalidHandleResponse(
      "The Controller returned a window outside the selected application.",
    );
  }
  return Object.freeze({
    kind: "window",
    pid: descriptor.pid,
    window_id: record.window_id,
    app: descriptor,
  });
}

export class ControllerError extends Error {
  constructor(message, options = {}) {
    super(message);
    this.name = "ControllerError";
    this.code = options.code || "controller_error";
    this.params = sanitizePublic(options.params || {});
    this.ambiguous = options.ambiguous === true;
  }
}

export class NotFound extends ControllerError {
  constructor(message, options = {}) {
    super(message, { ...options, code: options.code || "not_found" });
    this.name = "NotFound";
  }
}

export class Ambiguous extends ControllerError {
  constructor(message, options = {}) {
    super(message, { ...options, code: options.code || "ambiguous" });
    this.name = "Ambiguous";
  }
}

export class Timeout extends ControllerError {
  constructor(message, options = {}) {
    super(message, { ...options, code: options.code || "timeout" });
    this.name = "Timeout";
  }
}

export class TargetBusy extends ControllerError {
  constructor(message, options = {}) {
    super(message, { ...options, code: "target_busy" });
    this.name = "TargetBusy";
  }
}

// Keep the released SDK export working while the wire code moves to target_busy.
export const DesktopBusy = TargetBusy;

// Foreground input needs no consent and is never refused for changing focus,
// so nothing throws this. It stays exported so released scripts still load.
export class ForegroundConsentRequired extends ControllerError {
  constructor(message, options = {}) {
    super(message, {
      ...options,
      code: options.code || "foreground_consent_required",
    });
    this.name = "ForegroundConsentRequired";
  }
}

export class Unavailable extends ControllerError {
  constructor(message, options = {}) {
    super(message, { ...options, code: options.code || "unavailable" });
    this.name = "Unavailable";
  }
}

export class ConfirmRefused extends ControllerError {
  constructor(message, options = {}) {
    super(message, { ...options, code: options.code || "confirm_refused" });
    this.name = "ConfirmRefused";
  }
}

function publicError(error) {
  if (error instanceof ControllerError) {
    return error;
  }
  if (!(error instanceof ControllerFault)) {
    return error;
  }
  const options = {
    code: error.code,
    params: error.params,
    ambiguous: error.ambiguous,
  };
  if (error.code === "target_busy" || error.code === "desktop_busy") {
    return new TargetBusy(error.message, options);
  }
  if (
    error.code === "timeout" ||
    error.code === "script_timeout" ||
    error.code === "controller_timeout"
  ) {
    return new Timeout(error.message, options);
  }
  if (NOT_FOUND_CODES.has(error.code)) {
    return new NotFound(error.message, options);
  }
  if (
    error.code === "computer_use_unavailable" ||
    error.code === "platform_unsupported" ||
    error.code === "upstream_unavailable" ||
    error.code === "controller_unavailable"
  ) {
    return new Unavailable(error.message, options);
  }
  if (error.code === "confirm_refused") {
    return new ConfirmRefused(error.message, options);
  }
  return new ControllerError(error.message, options);
}

async function sdkStep(label, operation) {
  try {
    return await runStep(label, operation);
  } catch (error) {
    const mapped = publicError(error);
    if (mapped && typeof mapped === "object" && !mapped.step) {
      mapped.step = label;
    }
    throw mapped;
  }
}

function structured(result) {
  return result &&
    result.structuredContent &&
    typeof result.structuredContent === "object"
    ? result.structuredContent
    : {};
}

function observationHasScreenshot(result) {
  const payload = structured(result);
  // The driver may return capture metadata alongside an explicitly invalid
  // frame. That metadata does not authorize pixel coordinates.
  if (payload.screenshot_frame_valid === false) {
    return false;
  }
  if (
    typeof payload.screenshot_file_path === "string" &&
    payload.screenshot_file_path
  ) {
    return true;
  }
  return (
    Array.isArray(result?.content) &&
    result.content.some(
      (item) =>
        item &&
        item.type === "image" &&
        typeof item.data === "string" &&
        item.data.length > 0 &&
        (!item.mimeType || item.mimeType === "image/png"),
    )
  );
}

function effectResult(result) {
  const payload = sanitizePublic(structured(result));
  const status = EFFECT_STATUSES.has(payload.effect)
    ? payload.effect
    : "unverifiable";
  return { ...payload, effect: status, status };
}

function validateInteractionMessage(message) {
  if (typeof message !== "string" || !message.trim()) {
    throw new ControllerError(
      "Computer script messages must be non-empty strings.",
      {
        code: "invalid_computer_script_message",
        params: { field: "message" },
        ambiguous: false,
      },
    );
  }
}

function publicObservation(result) {
  return sanitizePublic(structured(result));
}

function sanitizePublic(value, seen = new WeakSet()) {
  if (Array.isArray(value)) {
    if (seen.has(value)) {
      return "[Circular]";
    }
    seen.add(value);
    const result = value.map((item) => sanitizePublic(item, seen));
    seen.delete(value);
    return result;
  }
  if (!value || typeof value !== "object") {
    return value;
  }
  if (seen.has(value)) {
    return "[Circular]";
  }
  seen.add(value);
  const result = Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => key !== "element_token")
      .map(([key, item]) => [key, sanitizePublic(item, seen)]),
  );
  seen.delete(value);
  return result;
}

function normalizeRole(value) {
  return String(value || "")
    .replace(/^AX/i, "")
    .toLowerCase();
}

function roleMatches(actual, expected) {
  if (expected === undefined) {
    return true;
  }
  const actualRole = normalizeRole(actual);
  const expectedRole = normalizeRole(expected);
  if (expectedRole === "textbox") {
    return actualRole === "textfield" || actualRole === "textarea";
  }
  return actualRole === expectedRole;
}

function invalidSelector(noun, reason, field) {
  throw new ControllerError(`${noun} selector ${reason}.`, {
    code: "invalid_selector",
    params: field === undefined ? {} : { field },
  });
}

function validateSelector(selector, allowedFields, noun, depth = 0) {
  if (
    !selector ||
    typeof selector !== "object" ||
    Array.isArray(selector)
  ) {
    invalidSelector(noun, "must be an object");
  }
  if (depth > 8) {
    invalidSelector(noun, "exceeds the supported within depth");
  }
  for (const field of Object.keys(selector)) {
    if (!allowedFields.has(field)) {
      invalidSelector(noun, `contains unknown field '${field}'`, field);
    }
  }
  if (
    selector.index !== undefined &&
    (!Number.isInteger(selector.index) || selector.index < 0)
  ) {
    invalidSelector(noun, "index must be a non-negative integer", "index");
  }
  if (
    selector.windowId !== undefined &&
    (!Number.isInteger(selector.windowId) || selector.windowId < 0)
  ) {
    invalidSelector(noun, "windowId must be a non-negative integer", "windowId");
  }
  if (
    selector.onScreen !== undefined &&
    typeof selector.onScreen !== "boolean"
  ) {
    invalidSelector(noun, "onScreen must be a boolean", "onScreen");
  }
  if (selector.within !== undefined) {
    validateSelector(
      selector.within,
      ELEMENT_SELECTOR_FIELDS,
      "Element",
      depth + 1,
    );
  }
  return selector;
}

function exactString(actual, expected) {
  return expected === undefined || actual === expected;
}

function containsString(actual, expected) {
  return (
    expected === undefined ||
    String(actual ?? "").toLowerCase().includes(String(expected).toLowerCase())
  );
}

function baseMatches(record, selector, fields) {
  return (
    roleMatches(fields.role(record), selector.role) &&
    exactString(fields.label(record), selector.label) &&
    containsString(fields.label(record), selector.labelContains) &&
    exactString(fields.value(record), selector.value)
  );
}

function selectOne(records, selector, noun) {
  let matches = records;
  if (selector.index !== undefined) {
    if (!Number.isInteger(selector.index) || selector.index < 0) {
      invalidSelector(noun, "index must be a non-negative integer", "index");
    }
    matches =
      selector.index < matches.length ? [matches[selector.index]] : [];
  }
  if (matches.length === 0) {
    throw new NotFound(`No ${noun} matched the selector.`, {
      params: { selector },
    });
  }
  if (matches.length > 1) {
    throw new Ambiguous(`${matches.length} ${noun}s matched the selector.`, {
      params: { selector, count: matches.length },
    });
  }
  return matches[0];
}

function appMatches(record, selector) {
  return (
    baseMatches(record, selector, {
      role: () => "application",
      label: (item) => item.name,
      value: (item) => item.bundle_id,
    }) &&
    exactString(record.bundle_id, selector.bundleId) &&
    exactString(record.name, selector.name) &&
    containsString(record.name, selector.nameContains)
  );
}

function windowMatches(record, selector) {
  return (
    baseMatches(record, selector, {
      role: () => "AXWindow",
      label: (item) => item.title,
      value: (item) => item.title,
    }) &&
    exactString(record.title, selector.title) &&
    containsString(record.title, selector.titleContains) &&
    exactString(record.window_id, selector.windowId) &&
    exactString(record.is_on_screen, selector.onScreen)
  );
}

function selectWindow(records, selector) {
  const matches = records.filter((item) => windowMatches(item, selector));
  if (selector.index === undefined) {
    return selectOne(matches, {}, "window");
  }
  if (selector.index >= matches.length) {
    return selectOne([], {}, "window");
  }
  if (matches.length === 1) {
    return matches[0];
  }
  const zIndexes = matches.map((item) => item.z_index);
  if (
    zIndexes.some((value) => !Number.isInteger(value)) ||
    new Set(zIndexes).size !== zIndexes.length
  ) {
    throw new ControllerError(
      "Window order is unavailable for this selector.",
      {
        code: "window_order_unavailable",
        params: { count: matches.length },
      },
    );
  }
  return [...matches].sort((left, right) => right.z_index - left.z_index)[
    selector.index
  ];
}

function matchingElements(elements, selector, depth = 0) {
  validateSelector(selector, ELEMENT_SELECTOR_FIELDS, "Element", depth);
  const byIndex = new Map(
    elements
      .filter((item) => Number.isInteger(item.element_index))
      .map((item) => [item.element_index, item]),
  );
  let withinIndexes;
  if (selector.within !== undefined) {
    const boundary = selectOne(
      matchingElements(elements, selector.within, depth + 1),
      {},
      "within boundary",
    );
    withinIndexes = new Set([boundary.element_index]);
  }
  let matches = elements.filter((item) => {
    if (
      !baseMatches(item, selector, {
        role: (record) => record.role,
        label: (record) => record.label,
        value: (record) => record.value,
      })
    ) {
      return false;
    }
    if (!withinIndexes) {
      return true;
    }
    const visited = new Set();
    let parentIndex = item.parent_index;
    while (Number.isInteger(parentIndex) && !visited.has(parentIndex)) {
      if (withinIndexes.has(parentIndex)) {
        return true;
      }
      visited.add(parentIndex);
      parentIndex = byIndex.get(parentIndex)?.parent_index;
    }
    return false;
  });
  if (selector.index !== undefined) {
    matches =
      Number.isInteger(selector.index) &&
      selector.index >= 0 &&
      selector.index < matches.length
        ? [matches[selector.index]]
        : [];
  }
  return matches;
}

function selectElement(elements, selector) {
  return selectOne(matchingElements(elements, selector), {}, "element");
}

function timeoutMs(value) {
  const candidate =
    typeof value === "number"
      ? value
      : value && typeof value === "object"
        ? value.timeout
        : undefined;
  if (candidate === undefined) {
    return DEFAULT_WAIT_MS;
  }
  if (!Number.isFinite(candidate) || candidate < 0) {
    throw new Timeout("Timeout must be a non-negative number.", {
      code: "invalid_timeout",
    });
  }
  return candidate;
}

async function poll(label, timeoutValue, attempt) {
  const duration = timeoutMs(timeoutValue);
  const deadline = performance.now() + duration;
  let firstAttempt = true;
  while (true) {
    const localRemaining = deadline - performance.now();
    if (!firstAttempt && localRemaining <= 0) {
      throw new Timeout(`${label} timed out after ${duration} ms.`, {
        params: { timeout: duration },
      });
    }
    const attemptDeadline =
      firstAttempt && duration === 0 ? undefined : deadline;
    firstAttempt = false;
    try {
      return await attempt(attemptDeadline);
    } catch (error) {
      if (!(error instanceof NotFound)) {
        throw error;
      }
    }
    await sleepWithinDeadline(
      Math.min(POLL_INTERVAL_MS, Math.max(0, deadline - performance.now())),
    );
  }
}

async function callToolBefore(tool, argumentsValue, deadline, options = {}) {
  if (deadline === undefined) {
    return callTool(tool, argumentsValue, options);
  }
  const remaining = deadline - performance.now();
  if (remaining <= 0) {
    throw new Timeout("The operation deadline expired.");
  }
  return callTool(tool, argumentsValue, { ...options, timeoutMs: remaining });
}

function callHandleTool(handle, tool, argumentsValue, deadline) {
  return callToolBefore(tool, argumentsValue, deadline, {
    descriptor: handle._descriptor,
  });
}

function shouldRetryElementAction(error) {
  return (
    error instanceof ControllerFault &&
    error.dispatched === false &&
    STALE_ELEMENT_CODES.has(error.code)
  );
}

function windowArguments(handle, options = {}) {
  return { ...options, pid: handle.pid, window_id: handle.windowId };
}

class Computer {
  constructor() {
    this.clipboard = Object.freeze({
      read: (options = {}) =>
        sdkStep("computer.clipboard.read", async () =>
          structured(await callTool("clipboard_read", options)),
        ),
      write: (text) =>
        sdkStep("computer.clipboard.write", async () =>
          effectResult(await callTool("clipboard_write", { text })),
        ),
    });
  }

  async app(selector = {}) {
    return sdkStep("computer.app", async () => this._resolveApp(selector));
  }

  async waitFor(selector = {}, options) {
    return sdkStep("computer.waitFor", async () =>
      poll("Application wait", options, (deadline) =>
        this._resolveApp(selector, deadline),
      ),
    );
  }

  async _resolveApp(selector = {}, deadline) {
    validateSelector(selector, APP_SELECTOR_FIELDS, "Application");
    const apps = structured(await callToolBefore("list_apps", {}, deadline)).apps;
    const records = Array.isArray(apps) ? apps : [];
    const record = selectOne(
      records.filter((item) => appMatches(item, selector)),
      selector,
      "application",
    );
    return new AppHandle(record);
  }
}

class AppHandle {
  constructor(record) {
    const descriptor = appDescriptor(record);
    this.pid = descriptor.pid;
    this.name =
      typeof record.name === "string" && record.name
        ? record.name
        : descriptor.name;
    this.bundleId = descriptor.bundle_id;
    Object.defineProperty(this, "_descriptor", { value: descriptor });
    Object.freeze(this);
  }

  async launch(options = {}) {
    return sdkStep("app.launch", async () => {
      const descriptor = this._descriptor;
      const argumentsValue = { ...options };
      if (this.bundleId) {
        argumentsValue.bundle_id = this.bundleId;
      } else {
        argumentsValue.name = this.name;
      }
      const result = await callHandleTool(this, "launch_app", argumentsValue);
      const payload = structured(result);
      const pid =
        Number.isInteger(payload.pid) && payload.pid > 0 ? payload.pid : null;
      let record;
      if (pid === null) {
        const apps = structured(await callTool("list_apps", {})).apps;
        const records = Array.isArray(apps) ? apps : [];
        record = selectOne(
          records.filter(
            (item) =>
              Number.isInteger(item.pid) &&
              item.pid > 0 &&
              (descriptor.bundle_id !== null
                ? item.bundle_id === descriptor.bundle_id
                : item.name === descriptor.name),
          ),
          {},
          "launched application",
        );
      } else {
        record = {
          pid,
          bundle_id: descriptor.bundle_id,
          name:
            typeof payload.name === "string" && payload.name
              ? payload.name
              : this.name,
        };
      }
      return {
        ...effectResult(result),
        app: new AppHandle(record),
      };
    });
  }

  async forceQuit() {
    return sdkStep("app.forceQuit", async () => {
      const descriptor = this._descriptor;
      return effectResult(
        await callHandleTool(this, "kill_app", { pid: descriptor.pid }),
      );
    });
  }

  async window(selector = {}) {
    return sdkStep("app.window", async () => this._resolveWindow(selector));
  }

  async waitFor(selector = {}, options) {
    return sdkStep("app.waitFor", async () =>
      poll("Window wait", options, (deadline) =>
        this._resolveWindow(selector, deadline),
      ),
    );
  }

  async _resolveWindow(selector, deadline) {
    validateSelector(selector, WINDOW_SELECTOR_FIELDS, "Window");
    const descriptor = this._descriptor;
    const windows = structured(
      await callHandleTool(this, "list_windows", { pid: descriptor.pid }, deadline),
    ).windows;
    const records = Array.isArray(windows) ? windows : [];
    const record = selectWindow(records, selector);
    return new WindowHandle(this, selector, record);
  }
}

class WindowHandle {
  constructor(app, _selector, record) {
    windowState.set(this, {
      snapshot: undefined,
      snapshotKey: undefined,
      snapshotHasScreenshot: false,
    });
    const descriptor = windowDescriptor(app, record);
    this.pid = descriptor.pid;
    this.windowId = descriptor.window_id;
    this.title = record.title;
    Object.defineProperty(this, "_descriptor", { value: descriptor });
    Object.freeze(this);
  }

  element(selector) {
    return new ElementHandle(this, selector);
  }

  expect(selector) {
    return new ElementExpectation(this, selector);
  }

  async waitFor(selector, options) {
    return sdkStep("window.waitFor", async () => {
      await poll("Element wait", options, (deadline) =>
        resolveWindowElement(this, selector, true, deadline),
      );
      return new ElementHandle(this, selector);
    });
  }

  async observe(options = {}) {
    return sdkStep("window.observe", async () =>
      publicObservation(await observeWindow(this, true, options)),
    );
  }

  // Bring this exact window to the front and leave it there. The previous
  // frontmost window is not restored.
  async activate() {
    return sdkStep("window.activate", async () =>
      effectResult(
        await callHandleTool(this, "bring_to_front", windowArguments(this)),
      ),
    );
  }

  async click(options) {
    return this._windowAction("window.click", "click", options);
  }

  async type(text, options = {}) {
    return this._windowAction("window.type", "type_text", {
      ...options,
      text,
    });
  }

  async press(key, options = {}) {
    return this._windowAction("window.press", "press_key", {
      ...options,
      key,
    });
  }

  async hotkey(keys, options = {}) {
    return this._windowAction("window.hotkey", "hotkey", {
      ...options,
      keys,
    });
  }

  async scroll(direction, options = {}) {
    return this._windowAction("window.scroll", "scroll", {
      ...options,
      direction,
    });
  }

  async drag(options) {
    return this._windowAction("window.drag", "drag", options);
  }

  async _windowAction(label, tool, options = {}) {
    return sdkStep(label, async () => {
      // The driver admits screenshot pixels only against a current snapshot
      // that carries a screenshot; element resolution leaves a tree-only one.
      await observeWindow(
        this,
        addressesPixels(options) &&
          !windowState.get(this).snapshotHasScreenshot,
      );
      return effectResult(
        await callHandleTool(this, tool, windowArguments(this, options)),
      );
    });
  }
}

const PIXEL_FIELDS = ["x", "y", "from_x", "from_y", "to_x", "to_y"];

function addressesPixels(options) {
  return PIXEL_FIELDS.some((field) => options[field] !== undefined);
}

async function observeWindow(handle, force, options = {}, deadline) {
  const state = windowState.get(handle);
  const descriptor = handle._descriptor;
  const key = `${descriptor.pid}:${descriptor.window_id}`;
  if (!force && state.snapshot && state.snapshotKey === key) {
    return state.snapshot;
  }
  const observation = await callHandleTool(
    handle,
    "get_window_state",
    windowArguments(handle, options),
    deadline,
  );
  state.snapshot = observation;
  state.snapshotKey = key;
  state.snapshotHasScreenshot = observationHasScreenshot(observation);
  return observation;
}

// Element resolution reads only the accessibility tree. Element actions act
// through element tokens, so they skip the screen capture and its encoding.
const ELEMENT_RESOLUTION_OPTIONS = Object.freeze({ include_screenshot: false });

async function resolveWindowElement(handle, selector, force, deadline) {
  validateSelector(selector, ELEMENT_SELECTOR_FIELDS, "Element");
  const observation = await observeWindow(
    handle,
    force,
    ELEMENT_RESOLUTION_OPTIONS,
    deadline,
  );
  const elements = structured(observation).elements;
  return selectElement(Array.isArray(elements) ? elements : [], selector);
}

async function runElementAction(handle, selector, label, tool, options = {}) {
  return sdkStep(label, async () => {
    let element = await resolveWindowElement(handle, selector, true);
    if (typeof element.element_token !== "string" || !element.element_token) {
      throw new NotFound("The matched element is not actionable.", {
        code: "element_not_actionable",
      });
    }
    const invoke = () =>
      callHandleTool(
        handle,
        tool,
        windowArguments(handle, {
          ...options,
          element_token: element.element_token,
        }),
      );
    try {
      return effectResult(await invoke());
    } catch (error) {
      if (!shouldRetryElementAction(error)) {
        throw error;
      }
      element = await resolveWindowElement(handle, selector, true);
      if (typeof element.element_token !== "string" || !element.element_token) {
        throw new NotFound("The matched element is not actionable.", {
          code: "element_not_actionable",
        });
      }
      return effectResult(await invoke());
    }
  });
}

class ElementHandle {
  constructor(window, selector) {
    this._window = window;
    this._selector = selector;
  }

  expect() {
    return new ElementExpectation(this._window, this._selector);
  }

  click(options = {}) {
    return runElementAction(
      this._window,
      this._selector,
      "element.click",
      "click",
      options,
    );
  }

  doubleClick(options = {}) {
    return runElementAction(
      this._window,
      this._selector,
      "element.doubleClick",
      "double_click",
      options,
    );
  }

  rightClick(options = {}) {
    return runElementAction(
      this._window,
      this._selector,
      "element.rightClick",
      "click",
      { ...options, button: "right" },
    );
  }

  fill(value) {
    return runElementAction(
      this._window,
      this._selector,
      "element.fill",
      "set_value",
      { value },
    );
  }

  type(text, options = {}) {
    return runElementAction(
      this._window,
      this._selector,
      "element.type",
      "type_text",
      { ...options, text },
    );
  }

  press(key, options = {}) {
    return runElementAction(
      this._window,
      this._selector,
      "element.press",
      "press_key",
      { ...options, key },
    );
  }

  hotkey(keys, options = {}) {
    return runElementAction(
      this._window,
      this._selector,
      "element.hotkey",
      "hotkey",
      { ...options, keys },
    );
  }

  scroll(direction, options = {}) {
    return runElementAction(
      this._window,
      this._selector,
      "element.scroll",
      "scroll",
      { ...options, direction },
    );
  }
}

class ElementExpectation {
  constructor(window, selector) {
    this._window = window;
    this._selector = selector;
  }

  toExist(options) {
    return this._check("expect.toExist", options, () => true);
  }

  toHaveValue(value, options) {
    return this._check(
      "expect.toHaveValue",
      options,
      (element) => element.value === value,
    );
  }

  toHaveLabel(label, options) {
    return this._check(
      "expect.toHaveLabel",
      options,
      (element) => String(element.label ?? "") === String(label),
    );
  }

  toBeEnabled(options) {
    return this._check(
      "expect.toBeEnabled",
      options,
      (element) => element.enabled === true,
    );
  }

  toBeSelected(options) {
    return this._check(
      "expect.toBeSelected",
      options,
      (element) => element.selected === true,
    );
  }

  async _check(label, options, predicate) {
    return sdkStep(label, async () =>
      poll("Expectation", options, async (deadline) => {
        const element = await resolveWindowElement(
          this._window,
          this._selector,
          true,
          deadline,
        );
        if (!predicate(element)) {
          throw new NotFound("The expectation is not satisfied yet.");
        }
      }),
    );
  }
}

export const computer = Object.freeze(new Computer());

export const avibe = Object.freeze({
  notify(message) {
    return sdkStep("avibe.notify", async () => {
      validateInteractionMessage(message);
      return effectResult(await callScriptInteraction("notify", message));
    });
  },

  confirm(message) {
    return sdkStep("avibe.confirm", async () => {
      validateInteractionMessage(message);
      return effectResult(await callScriptInteraction("confirm", message));
    });
  },
});
