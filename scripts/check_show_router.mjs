#!/usr/bin/env node
// Exercise the real Python initializer against the real Runtime HTTP renderer.
import assert from "node:assert/strict"
import { execFileSync } from "node:child_process"
import { mkdtemp, readFile, rm, writeFile, mkdir, stat } from "node:fs/promises"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"
import { parseArgs } from "node:util"

const repository = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const { values } = parseArgs({
  options: {
    "runtime-root": { type: "string" },
    python: { type: "string", default: "python3" },
    "legacy-router-ssr": { type: "boolean", default: false }
  }
})
if (!values["runtime-root"]) throw new Error("--runtime-root is required")
const runtimeRoot = resolve(values["runtime-root"])
const { startShowRuntimeServer } = await import(pathToFileURL(join(runtimeRoot, "packages/runtime/dist/server.js")).href)
const temporary = await mkdtemp(join(tmpdir(), "avibe-python-router-check-"))
const executablePath = process.env.PATH
for (const key of Object.keys(process.env)) delete process.env[key]
Object.assign(process.env, {
  PATH: executablePath,
  HOME: join(temporary, "home"),
  AVIBE_HOME: temporary,
  XDG_CONFIG_HOME: join(temporary, "config"),
  XDG_DATA_HOME: join(temporary, "data"),
  XDG_CACHE_HOME: join(temporary, "cache-home"),
  XDG_STATE_HOME: join(temporary, "state-home"),
  AVIBE_ALLOW_DEV_STATE_MIGRATION: "1"
})
const workspaceRoot = join(temporary, "show")
const sessions = ["sesfresh", "seslegacylf", "seslegacycrlf"]
const originalRouters = new Map()
let server
async function routerSnapshot(workspace) {
  const path = join(workspace, "src/router.tsx")
  const info = await stat(path)
  return {
    source: await readFile(path, "utf8"),
    inode: info.ino,
    mode: info.mode,
    mtimeMs: info.mtimeMs
  }
}
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
  for (const sessionId of sessions) {
    initialize(sessionId)
    const workspace = join(workspaceRoot, sessionId)
    if (sessionId !== "sesfresh") {
      const legacy = await readFile(
        join(repository, "tests/fixtures/show_pages/router-history-pre-ssr.tsx"), "utf8"
      )
      await writeFile(join(workspace, "src/router.tsx"),
        sessionId === "seslegacycrlf" ? legacy.replaceAll("\n", "\r\n") : legacy)
    }
    originalRouters.set(sessionId, await routerSnapshot(workspace))
    // Exercise initialization for LF; CRLF is a Markdown-first legacy access.
    if (sessionId === "seslegacylf") initialize(sessionId)
    if (sessionId !== "sesfresh") {
      // Stock routers can have pages that consume the released exports and
      // root fallback. Compatibility must preserve those existing callers.
      await writeFile(join(workspace, "src/pages/index.tsx"), `
import { useContext } from "react"
import { MotionConfigContext } from "motion/react"
import { routes } from "../router"
export default function Home() {
  const motion = useContext(MotionConfigContext)
  return <main><h1>Building your Show Page</h1>
    <p>Dynamic routes: {routes.filter((route) => route.dynamic).length}</p>
    <p>Root location: {window.location.pathname}</p>
    <p>Root query: {new URLSearchParams(window.location.search).get("period")}</p>
    <p>Read-only location: {String(Object.isFrozen(window) && Object.isFrozen(window.location))}</p>
    <p>Static motion: {String(motion.isStatic)}</p></main>
}
`)
    }
    await mkdir(join(workspace, "src/pages/items"), { recursive: true })
    await writeFile(join(workspace, "src/pages/items/[id].tsx"), `
import { Link, ${sessionId === "sesfresh" ? "" : "routes, "}type PageProps } from "../../router"
export default function Item({ params, query }: PageProps) {
  return <main><h1>Item {params.id}</h1><p>View {query.get("view")}</p>
    ${sessionId === "sesfresh" ? "" : '<p>Legacy dynamic field: {String(routes.find((route) => route.path === "/items/:id")?.dynamic)}</p>'}
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
  for (const sessionId of sessions) {
    for (const basePath of [`/show/${sessionId}/`, "/p/public-share/"]) {
      const headers = {
        "x-vibe-show-base": basePath,
        "x-avibe-show-protocol": "1",
        "x-avibe-show-context": basePath.startsWith("/p/") ? "shared" : "private"
      }
      const rootPeriod = basePath.startsWith("/p/") ? "一季度" : "四季度"
      const targets = [
        [`/?period=${encodeURIComponent(rootPeriod)}`, "Building your Show Page"],
        ["/second", "# A second page"],
        ["/items/%E4%B8%AD%E6%96%87?view=%E5%91%A8&vibe-embed=1", "# Item 中文"]
      ]
      // Default-branch compatibility does not claim legacy nested SSR support.
      // The opt-in mode is the combined acceptance gate with Runtime PR #70.
      const nestedSupported = sessionId === "sesfresh" || values["legacy-router-ssr"]
      for (const [target, expected] of nestedSupported ? targets : targets.slice(0, 1)) {
        const response = await fetch(`${server.url}/sessions/${sessionId}/render-markdown`, {
          headers: { ...headers, "x-vibe-show-target": target }
        })
        const markdown = await response.text()
        assert.equal(response.status, 200, markdown)
        assert.match(response.headers.get("content-type"), /^text\/markdown/)
        assert(markdown.includes(expected), markdown)
        if (sessionId !== "sesfresh" && target.startsWith("/?")) {
          assert(markdown.includes("Dynamic routes: 1"), markdown)
          assert(markdown.includes(`Root location: ${basePath}`), markdown)
          assert(markdown.includes(`Root query: ${rootPeriod}`), markdown)
          assert(markdown.includes("Read-only location: true"), markdown)
          assert(markdown.includes("Static motion: true"), markdown)
        }
        if (target.startsWith("/items/")) {
          assert(markdown.includes("View 周"), markdown)
          assert(markdown.includes(`${basePath}second?from=detail&vibe-embed=1`), markdown)
          if (sessionId !== "sesfresh") {
            assert(markdown.includes("Legacy dynamic field: true"), markdown)
          }
        }
      }
      const html = await fetch(`${server.url}/sessions/${sessionId}/app/second`, {
        headers: { ...headers, Accept: "text/html" }
      })
      assert.equal(html.status, 200, await html.text())
      assert.match(html.headers.get("content-type"), /^text\/html/)
    }
    assert.deepEqual(await routerSnapshot(join(workspaceRoot, sessionId)), originalRouters.get(sessionId),
      `${sessionId}: initialization or rendering changed the editable router`)
  }
  console.log(`Show router integration passed: fresh nested SSR, Unicode, queries, links, HTML, private + public; legacy LF/CRLF ${values["legacy-router-ssr"] ? "nested SSR" : "root fallback"}, route fields, read-only root location + static motion; source files unchanged.`)
  if (!values["legacy-router-ssr"]) {
    execFileSync(values.python, ["-m", "pytest", "tests/test_show_api_integration.py", "-q"], {
      cwd: repository,
      env: { ...process.env, AVIBE_SHOW_API_RUNTIME_ROOT: runtimeRoot },
      stdio: "inherit"
    })
  }
} finally {
  await server?.close()
  await rm(temporary, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 })
}
