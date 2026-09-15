#!/usr/bin/env node
// The Runtime generator is the only authored implementation of the Show router.
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"
import { parseArgs } from "node:util"

const repository = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const { values } = parseArgs({
  options: {
    "runtime-module": { type: "string" },
    output: { type: "string", default: join(repository, "vibe", "show_router.tsx") },
    check: { type: "boolean", default: false }
  }
})
if (!values["runtime-module"]) throw new Error("--runtime-module must name the built Runtime templates.js")
const { ensureSessionTemplate } = await import(pathToFileURL(resolve(values["runtime-module"])).href)
const workspace = await mkdtemp(join(tmpdir(), "avibe-show-router-export-"))
try {
  await ensureSessionTemplate(workspace)
  const router = await readFile(join(workspace, "src", "router.tsx"), "utf8")
  if (!router.includes("export function SsrRouterProvider")) {
    throw new Error("The Runtime template must support SSR Markdown subroutes")
  }
  if (values.check) {
    if (await readFile(values.output, "utf8") !== router) {
      throw new Error("The packaged Show router differs from the Runtime template; run sync_show_router.mjs")
    }
  } else {
    await writeFile(values.output, router, "utf8")
  }
} finally {
  await rm(workspace, { recursive: true, force: true })
}
