import { register } from "node:module";

const stdoutWrite = process.stdout.write.bind(process.stdout);
let output;

try {
  const input = await readStdin();
  register(new URL("./loader.mjs", import.meta.url), {
    data: { sdkUrl: input.sdk_url },
  });

  const runtime = await import(new URL("./runtime.mjs", import.meta.url));
  runtime.configureRuntime(input);

  process.stdout.write = process.stderr.write.bind(process.stderr);
  console.log = console.error;
  console.info = console.error;
  console.debug = console.error;

  try {
    const result = await runBeforeDeadline(runtime, async () => {
      const module = await import(input.script_url);
      if (typeof module.default !== "function") {
        throw new Error("Computer Use scripts must default-export a function.");
      }
      return module.default(input.arguments);
    });
    const serializedResult = JSON.stringify(result ?? null);
    if (serializedResult === undefined) {
      throw new Error("Computer Use scripts must return a JSON value.");
    }
    output = JSON.stringify({ ok: true, result: JSON.parse(serializedResult) });
  } catch (error) {
    const context = runtime.failureContext(error);
    output = JSON.stringify({
      ok: false,
      error: {
        name: error?.name || "Error",
        code: error?.code || "computer_script_failed",
        message: error?.message || String(error),
        params: error?.params || {},
        step: context.step,
        screenshot_path: context.screenshot_path,
        ambiguous: error?.ambiguous === true,
      },
    });
  } finally {
    await runtime.closeScriptConnection().catch(() => {});
  }
} catch (error) {
  output = JSON.stringify({
    ok: false,
    error: {
      name: error?.name || "Error",
      code: error?.code || "computer_script_runtime_failed",
      message: error?.message || String(error),
      params: error?.params || {},
      step: "script",
    },
  });
}

stdoutWrite(output, (error) => {
  process.exit(error ? 1 : 0);
});

async function runBeforeDeadline(runtime, operation) {
  const remaining = runtime.remainingTimeMs();
  if (remaining <= 0) {
    throw new runtime.ControllerFault(
      "script_timeout",
      "The script deadline expired.",
    );
  }
  let timer;
  try {
    return await Promise.race([
      operation(),
      new Promise((_, reject) => {
        timer = setTimeout(
          () =>
            reject(
              new runtime.ControllerFault(
                "script_timeout",
                "The script deadline expired.",
              ),
            ),
          remaining,
        );
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  const parsed = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (
    !parsed ||
    typeof parsed !== "object" ||
    typeof parsed.script_url !== "string" ||
    typeof parsed.sdk_url !== "string" ||
    !parsed.arguments ||
    typeof parsed.arguments !== "object"
  ) {
    throw new Error("invalid Computer Use runner input");
  }
  return parsed;
}
