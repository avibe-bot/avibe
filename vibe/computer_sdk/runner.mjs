import { register } from "node:module";
import * as runtime from "./runtime.mjs";

const stdoutWrite = process.stdout.write.bind(process.stdout);
let output;

try {
  const input = await readStdin();
  register(new URL("./loader.mjs", import.meta.url), {
    data: { sdkUrl: input.sdk_url },
  });

  runtime.configureRuntime(input);

  process.stdout.write = process.stderr.write.bind(process.stderr);
  console.log = console.error;
  console.info = console.error;
  console.debug = console.error;

  try {
    const keepAlive = setInterval(() => {}, 1000);
    let result;
    try {
      const module = await import(input.script_url);
      if (typeof module.default !== "function") {
        throw new Error("Computer Use scripts must default-export a function.");
      }
      result = await module.default(input.arguments);
    } finally {
      clearInterval(keepAlive);
    }
    const serializedResult = JSON.stringify(result ?? null);
    if (serializedResult === undefined) {
      throw new Error("Computer Use scripts must return a JSON value.");
    }
    output = JSON.stringify({ ok: true, result: JSON.parse(serializedResult) });
  } catch (error) {
    output = JSON.stringify({
      ok: false,
      error: runtime.failureEnvelope(error),
    });
  } finally {
    await runtime.closeScriptConnection().catch(() => {});
  }
} catch (error) {
  output = JSON.stringify({
    ok: false,
    error: runtime.failureEnvelope(error, {
      defaultCode: "computer_script_runtime_failed",
    }),
  });
}

stdoutWrite(output, (error) => {
  process.exit(error ? 1 : 0);
});

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
