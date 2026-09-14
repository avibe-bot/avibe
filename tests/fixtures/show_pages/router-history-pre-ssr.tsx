import type { ComponentType, MouseEvent, ReactNode } from "react"
import { useSyncExternalStore } from "react"

export type PageProps = {
  // Values captured from [param] segments, e.g. { id: "42" } for /items/42.
  params: Record<string, string>
}

type PageModule = { default: ComponentType<PageProps> }

type Segment = { name: string; dynamic: boolean }
type Route = {
  path: string
  segments: Segment[]
  Component: ComponentType<PageProps>
  dynamic: boolean
}

const PAGES_PREFIX = "./pages/"
const PAGE_SUFFIX = ".tsx"

// Eagerly import every page module at build time. This is the discovery
// mechanism: a new file under src/pages/ automatically registers a route.
const modules = import.meta.glob<PageModule>("./pages/**/*.tsx", { eager: true })

// "./pages/items/[id].tsx" -> "/items/:id"; "./pages/index.tsx" -> "/".
// Returns null for framework files (any segment starting with "_"), which lets
// an agent colocate non-page helpers under src/pages/ without creating a route.
function filePathToRoute(file: string): string | null {
  const relative = file.slice(PAGES_PREFIX.length, file.length - PAGE_SUFFIX.length)
  const parts = relative.split("/")
  if (parts[parts.length - 1] === "index") parts.pop()
  if (parts.some((part) => part.startsWith("_"))) return null
  const path = parts
    .map((part) => (part.startsWith("[") && part.endsWith("]") ? ":" + part.slice(1, -1) : part))
    .join("/")
  return path ? "/" + path : "/"
}

function toSegments(path: string): Segment[] {
  if (path === "/") return []
  return path
    .slice(1)
    .split("/")
    .map((part) => (part.startsWith(":") ? { name: part.slice(1), dynamic: true } : { name: part, dynamic: false }))
}

// Per-segment specificity mask: "0" for a static segment, "1" for a dynamic one.
// Routes are sorted ascending by this mask (compared left to right), so among
// routes of the same length a static segment always beats a [param] at the same
// position — e.g. /items/new wins over /items/:id, and /users/:id/edit wins over
// /users/:id/:action.
function routeSpecificity(segments: Segment[]): string {
  return segments.map((segment) => (segment.dynamic ? "1" : "0")).join("")
}

// A page's default export is renderable if it is a function component or a React
// "exotic" component (memo/forwardRef/lazy/…) — an object carrying $$typeof.
// Rejecting by `typeof === "function"` alone would drop memo()/forwardRef() pages.
function isRenderablePage(value: unknown): value is ComponentType<PageProps> {
  return (
    typeof value === "function" ||
    (typeof value === "object" && value !== null && "$$typeof" in value)
  )
}

export const routes: Route[] = Object.entries(modules)
  .map(([file, mod]): Route | null => {
    const path = filePathToRoute(file)
    if (!path || !isRenderablePage(mod.default)) return null
    const segments = toSegments(path)
    return { path, segments, Component: mod.default, dynamic: segments.some((s) => s.dynamic) }
  })
  .filter((route): route is Route => route !== null)
  .sort((a, b) => {
    const specA = routeSpecificity(a.segments)
    const specB = routeSpecificity(b.segments)
    if (specA !== specB) return specA < specB ? -1 : 1
    return a.path.localeCompare(b.path)
  })

// decodeURIComponent throws on a malformed escape (e.g. a link built with a raw
// "%", like /items/50%); fall back to the raw segment so a bad param degrades to
// that page instead of throwing during render and blanking the whole app.
function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

function matchRoute(path: string): { route: Route | null; params: Record<string, string> } {
  const parts = path === "/" ? [] : path.slice(1).split("/")
  for (const route of routes) {
    if (route.segments.length !== parts.length) continue
    const params: Record<string, string> = {}
    let matched = true
    for (let i = 0; i < parts.length; i++) {
      const segment = route.segments[i]
      if (segment.dynamic) params[segment.name] = safeDecode(parts[i])
      else if (segment.name !== safeDecode(parts[i])) {
        matched = false
        break
      }
    }
    if (matched) return { route, params }
  }
  return { route: null, params: {} }
}

function basePath(): string {
  const configured = globalThis.__AVIBE_SHOW__?.basePath
  const fallback = window.location.pathname.match(/^\/(?:show|p)\/[^/]+\//)?.[0] || "/"
  const pathname = new URL(configured || fallback, window.location.origin).pathname
  const parts = pathname.split("/").filter(Boolean)
  return parts.length ? "/" + parts.join("/") + "/" : "/"
}

function normalizeRoutePath(path: string): string {
  const withLeadingSlash = path.startsWith("/") ? path : "/" + path
  return withLeadingSlash.length > 1 && withLeadingSlash.endsWith("/")
    ? withLeadingSlash.slice(0, -1)
    : withLeadingSlash
}

function readRoutePath(): string {
  const base = basePath()
  const pathname = window.location.pathname
  if (!pathname.startsWith(base)) return "/"
  const routePath = normalizeRoutePath("/" + pathname.slice(base.length))
  return routePath === "/index.html" ? "/" : routePath
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener("popstate", onChange)
  return () => window.removeEventListener("popstate", onChange)
}

export function useRoutePath(): string {
  return useSyncExternalStore(subscribe, readRoutePath, () => "/")
}

function routeUrl(to: string): URL {
  const normalizedTo = to.startsWith("/") ? to : "/" + to
  const route = new URL(normalizedTo, window.location.origin)
  const current = new URL(window.location.href)
  const target = new URL(basePath(), window.location.origin)
  const embed = current.searchParams.get("vibe-embed")
  target.pathname = basePath() + route.pathname.replace(/^\/+/, "")
  target.search = route.search
  if (embed && !target.searchParams.has("vibe-embed")) target.searchParams.set("vibe-embed", embed)
  target.hash = route.hash
  return target
}

export function navigate(to: string): void {
  const target = routeUrl(to)
  window.history.pushState({}, "", target)
  window.dispatchEvent(new PopStateEvent("popstate"))
}

export function Link({ to, className, children }: { to: string; className?: string; children: ReactNode }) {
  function onClick(event: MouseEvent<HTMLAnchorElement>) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault()
    navigate(to)
  }
  return <a href={routeUrl(to).toString()} className={className} onClick={onClick}>{children}</a>
}

export function RouterView() {
  const path = useRoutePath()
  const { route, params } = matchRoute(path)
  if (!route) {
    return (
      <div className="rounded-lg border border-border bg-card p-6 text-card-foreground">
        <h1 className="text-lg font-semibold">Page not found</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          No route matches <code className="rounded bg-muted px-1.5 py-0.5">{path}</code>.
        </p>
        <p className="mt-4 text-sm">
          <Link className="font-medium underline underline-offset-4" to="/">Back to Home</Link>
        </p>
      </div>
    )
  }
  const Page = route.Component
  return <Page params={params} />
}
