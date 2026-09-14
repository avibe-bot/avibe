#!/usr/bin/env node
// Exercise the real Python initializer against the real Runtime HTTP renderer.
import assert from "node:assert/strict"
import { execFileSync } from "node:child_process"
import { mkdtemp, readFile, rm, writeFile, mkdir } from "node:fs/promises"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"
import { parseArgs } from "node:util"

const repository = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const { values } = parseArgs({
  options: {
    "runtime-root": { type: "string" },
    python: { type: "string", default: "python3" }
  }
})
if (!values["runtime-root"]) throw new Error("--runtime-root is required")
const runtimeRoot = resolve(values["runtime-root"])
const { startShowRuntimeServer } = await import(pathToFileURL(join(runtimeRoot, "packages/runtime/dist/server.js")).href)
const temporary = await mkdtemp(join(tmpdir(), "avibe-python-router-check-"))
const workspaceRoot = join(temporary, "show")
let server
function initialize(sessionId) {
  execFileSync(values.python, [
    "-c",
    "import sys; from core.show_pages import ensure_show_page_dir; ensure_show_page_dir(sys.argv[1])",
    sessionId
  ], { cwd: repository, env: { ...process.env, AVIBE_HOME: temporary }, stdio: "pipe" })
}
try {
  execFileSync(process.execPath, [
    join(repository, "scripts/sync_show_router.mjs"),
    "--runtime-module", join(runtimeRoot, "packages/runtime/dist/templates.js"),
    "--check"
  ], { stdio: "inherit" })
  for (const sessionId of ["sesfresh", "sesmigrated"]) {
    initialize(sessionId)
    const workspace = join(workspaceRoot, sessionId)
    if (sessionId === "sesmigrated") {
      await writeFile(join(workspace, "src/router.tsx"), await readFile(
        join(repository, "tests/fixtures/show_pages/router-history-pre-ssr.tsx")
      ))
      initialize(sessionId)
    }
    await mkdir(join(workspace, "src/pages/items"), { recursive: true })
    await writeFile(join(workspace, "src/pages/items/[id].tsx"), `
import { Link, type PageProps } from "../../router"
export default function Item({ params, query }: PageProps) {
  return <main><h1>Item {params.id}</h1><p>View {query.get("view")}</p>
    <Link to="/second?from=detail">Second page</Link></main>
}
`)
  }
  server = await startShowRuntimeServer({
    workspaceRoot,
    dependencyRoot: runtimeRoot,
    cacheRoot: join(temporary, "cache"),
    idlePruneIntervalMs: 0
  })
  for (const sessionId of ["sesfresh", "sesmigrated"]) {
    for (const basePath of [`/show/${sessionId}/`, "/p/public-share/"]) {
      const headers = {
        "x-vibe-show-base": basePath,
        "x-avibe-show-protocol": "1",
        "x-avibe-show-context": basePath.startsWith("/p/") ? "shared" : "private"
      }
      for (const [target, expected] of [
        ["/", "Building your Show Page"],
        ["/second", "# A second page"],
        ["/items/%E4%B8%AD%E6%96%87?view=%E5%91%A8&vibe-embed=1", "# Item 中文"]
      ]) {
        const response = await fetch(`${server.url}/sessions/${sessionId}/render-markdown`, {
          headers: { ...headers, "x-vibe-show-target": target }
        })
        const markdown = await response.text()
        assert.equal(response.status, 200, markdown)
        assert.match(response.headers.get("content-type"), /^text\/markdown/)
        assert(markdown.includes(expected), markdown)
        if (target.startsWith("/items/")) {
          assert(markdown.includes("View 周"), markdown)
          assert(markdown.includes(`${basePath}second?from=detail&vibe-embed=1`), markdown)
        }
      }
      const html = await fetch(`${server.url}/sessions/${sessionId}/app/second`, {
        headers: { ...headers, Accept: "text/html" }
      })
      assert.equal(html.status, 200, await html.text())
      assert.match(html.headers.get("content-type"), /^text\/html/)
    }
  }
  console.log("Show router integration passed: fresh + migrated, root + nested, Unicode, queries, links, HTML, private + public.")
} finally {
  await server?.close()
  await rm(temporary, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 })
}
