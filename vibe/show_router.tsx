import type { ComponentType, MouseEvent, ReactNode } from "react"
import { createContext, useContext, useSyncExternalStore } from "react"

export type PageProps = {
  params: Record<string, string>
  query: URLSearchParams
}

export type SsrRouteLocation = {
  pathname: string
  search: string
  origin: string
  basePath: string
}

type PageModule = { default: ComponentType<PageProps> }
type Segment = { name: string; dynamic: boolean }
type Route = {
  path: string
  segments: Segment[]
  Component: ComponentType<PageProps>
}

const PAGES_PREFIX = "./pages/"
const PAGE_SUFFIX = ".tsx"
const modules = import.meta.glob<PageModule>("./pages/**/*.tsx", { eager: true })
const SsrRouterContext = createContext<SsrRouteLocation | null>(null)

export function SsrRouterProvider({
  location,
  children
}: {
  location: SsrRouteLocation
  children: ReactNode
}) {
  return <SsrRouterContext.Provider value={location}>{children}</SsrRouterContext.Provider>
}

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
  return path.slice(1).split("/").map((part) =>
    part.startsWith(":")
      ? { name: part.slice(1), dynamic: true }
      : { name: part, dynamic: false }
  )
}

function routeSpecificity(segments: Segment[]): string {
  return segments.map((segment) => (segment.dynamic ? "1" : "0")).join("")
}

function isRenderablePage(value: unknown): value is ComponentType<PageProps> {
  return typeof value === "function" || (typeof value === "object" && value !== null && "$$typeof" in value)
}

export const routes: Route[] = Object.entries(modules)
  .map(([file, mod]): Route | null => {
    const path = filePathToRoute(file)
    if (!path || !isRenderablePage(mod.default)) return null
    return { path, segments: toSegments(path), Component: mod.default }
  })
  .filter((route): route is Route => route !== null)
  .sort((a, b) => {
    const specA = routeSpecificity(a.segments)
    const specB = routeSpecificity(b.segments)
    if (specA !== specB) return specA < specB ? -1 : 1
    return a.path.localeCompare(b.path)
  })

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
    for (let index = 0; index < parts.length; index++) {
      const segment = route.segments[index]
      if (segment.dynamic) params[segment.name] = safeDecode(parts[index])
      else if (segment.name !== safeDecode(parts[index])) {
        matched = false
        break
      }
    }
    if (matched) return { route, params }
  }
  return { route: null, params: {} }
}

function basePath(location?: SsrRouteLocation | null): string {
  if (location) {
    const pathname = new URL(location.basePath, location.origin).pathname
    const parts = pathname.split("/").filter(Boolean)
    return parts.length ? "/" + parts.join("/") + "/" : "/"
  }
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

function readRoutePath(location?: SsrRouteLocation | null): string {
  if (location) return normalizeRoutePath(location.pathname)
  const base = basePath(location)
  const pathname = window.location.pathname
  if (!pathname.startsWith(base)) return "/"
  const routePath = normalizeRoutePath("/" + pathname.slice(base.length))
  return routePath === "/index.html" ? "/" : routePath
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener("popstate", onChange)
  return () => window.removeEventListener("popstate", onChange)
}

function noSubscribe(): () => void {
  return () => undefined
}

export function useRoutePath(): string {
  const location = useContext(SsrRouterContext)
  return useSyncExternalStore(
    location ? noSubscribe : subscribe,
    location ? () => readRoutePath(location) : readRoutePath,
    () => readRoutePath(location)
  )
}

export function useRouteQuery(): URLSearchParams {
  const location = useContext(SsrRouterContext)
  const search = useSyncExternalStore(
    location ? noSubscribe : subscribe,
    location ? () => location.search : () => window.location.search,
    () => location?.search ?? ""
  )
  return new URLSearchParams(search)
}

function routeUrl(to: string, location?: SsrRouteLocation | null): URL {
  const normalizedTo = to.startsWith("/") ? to : "/" + to
  const origin = location?.origin ?? window.location.origin
  const route = new URL(normalizedTo, origin)
  const currentSearch = location?.search ?? new URL(window.location.href).search
  const target = new URL(basePath(location), origin)
  const embed = new URLSearchParams(currentSearch).get("vibe-embed")
  target.pathname = basePath(location) + route.pathname.replace(/^\/+/, "")
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
  const location = useContext(SsrRouterContext)
  function onClick(event: MouseEvent<HTMLAnchorElement>) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault()
    navigate(to)
  }
  return <a href={routeUrl(to, location).toString()} className={className} onClick={onClick}>{children}</a>
}

export function RouterView() {
  const path = useRoutePath()
  const query = useRouteQuery()
  const { route, params } = matchRoute(path)
  if (!route) {
    return (
      <div className="rounded-lg border border-border bg-card p-6 text-card-foreground">
        <h1 className="text-lg font-semibold">Page not found</h1>
        <p className="mt-2 text-sm text-muted-foreground">No route matches {path}.</p>
        <p className="mt-4 text-sm"><Link className="font-medium underline" to="/">Back to Home</Link></p>
      </div>
    )
  }
  const Page = route.Component
  return <Page params={params} query={query} />
}
