import { register } from "node:module";
import * as runtime from "./runtime.mjs";

const resultPath = process.env.AVIBE_COMPUTER_RESULT_PATH;
let output;

try {
  if (typeof resultPath !== "string" || !resultPath) {
    throw new Error("missing Computer Use runner result path");
  }
  const input = await readStdin();
  register(new URL("./loader.mjs", import.meta.url), {
    data: { sdkUrl: input.sdk_url },
  });

  runtime.configureRuntime(input);

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
    output = { ok: true, result: JSON.parse(serializedResult) };
  } catch (error) {
    output = {
      ok: false,
      error: runtime.failureEnvelope(error),
    };
  } finally {
    await runtime.closeScriptConnection().catch(() => {});
  }
} catch (error) {
  output = {
    ok: false,
    error: runtime.failureEnvelope(error, {
      defaultCode: "computer_script_runtime_failed",
    }),
  };
}

try {
  runtime.writeCappedJsonFile(resultPath, output);
  process.exit(0);
} catch (error) {
  if (error instanceof runtime.ProtocolFileTooLargeError) {
    const protocolError = new Error(
      "The script result exceeded the protocol size limit.",
    );
    protocolError.name = "ControllerError";
    protocolError.code = "computer_script_protocol_error";
    protocolError.params = { limit_bytes: error.limitBytes };
    protocolError.ambiguous = true;
    try {
      runtime.writeCappedJsonFile(resultPath, {
        ok: false,
        error: runtime.failureEnvelope(protocolError),
      });
      process.exit(0);
    } catch (fallbackError) {
      console.error(fallbackError);
    }
  } else {
    console.error(error);
  }
  process.exit(1);
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
