import { spawn } from "node:child_process"
import { isAbsolute, join } from "node:path"

export const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const MAX_BYTES = 32768
let active = 0

export function response(body: unknown, status: number): Response {
  return Response.json(body, { status, headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" } })
}

export async function boundedBody(request: Request): Promise<Uint8Array | null> {
  const reader = request.body?.getReader()
  if (!reader) return new Uint8Array()
  const chunks: Uint8Array[] = []
  let length = 0
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      length += value.length
      if (length > MAX_BYTES) { await reader.cancel(); return null }
      chunks.push(value)
    }
    return Buffer.concat(chunks)
  } finally { reader.releaseLock() }
}

export async function runWorker(args: string[], input?: Uint8Array): Promise<Record<string, any>> {
  // Explicit operator path: Runtime may bundle server modules into a cache.
  const root = process.env.AVIBE_FEEDBACK_APP_ROOT
  if (!root || !isAbsolute(root)) return { status: 503 }
  if (active >= 4) return { status: 429 }
  active++
  const environment = { ...process.env }
  for (const name of ["AVIBE_FEEDBACK_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"]) delete environment[name]
  try {
    return await new Promise((resolve) => {
      const child = spawn(process.env.AVIBE_FEEDBACK_PYTHON || "python3", [join(root, "worker.py"), ...args], {
        cwd: root, env: environment, stdio: ["pipe", "pipe", "ignore"], shell: false,
      })
      let failed = false
      let length = 0
      const chunks: Buffer[] = []
      const timer = setTimeout(() => {
        failed = true
        child.kill("SIGKILL")
        resolve({ status: 504 })
      }, input === undefined ? 5_000 : 24_000)
      child.stdin.on("error", () => { /* Early child exit is handled by close. */ })
      child.stdout.on("data", (chunk: Buffer) => {
        length += chunk.length
        if (length > MAX_BYTES) { failed = true; child.kill("SIGKILL") }
        else chunks.push(chunk)
      })
      child.on("error", () => { clearTimeout(timer); resolve({ status: 503 }) })
      child.on("close", (code) => {
        clearTimeout(timer)
        if (!failed && code === 0) {
          try {
            const value = JSON.parse(Buffer.concat(chunks).toString("utf8"))
            if (Number.isInteger(value.status) && value.status >= 200 && value.status <= 599) { resolve(value); return }
          } catch { /* Malformed child output fails closed. */ }
        }
        resolve({ status: 503 })
      })
      child.stdin.end(input)
    })
  } finally { active-- }
}
