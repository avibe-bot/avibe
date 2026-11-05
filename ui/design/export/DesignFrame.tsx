// avibe-design-frame: shows one design frame live in a Show Page.
// Written by `npm run design:export` in avibe/ui and in avibe-bot-backend (the same file in both); it
// imports nothing from the product, only the files the exports put in public/design/: each
// repository's live canvas, screenshots, and one shared frames.json, whose entries name their canvas.
//
//   import { DesignFrame } from "../design/DesignFrame"
//   <DesignFrame id="sidebar/bots/three" />                 light, then dark
//   <DesignFrame id="sidebar/bots/three" themes={["dark"]} />
//
// A live frame is an iframe, which the Show Page's annotation overlay cannot reach. While
// annotation is on, each frame is drawn as its screenshot instead, in the same place and size,
// so it can be annotated like any image; the image's alt names the frame and theme. A frame whose
// state takes a hover, click or press (`interaction`) is always drawn as its screenshot, which shows that
// state; "单独打开" still opens it live.
import { useEffect, useRef, useState } from "react"

// `canvas`: the folder under public/design/ that holds the frame's live canvas; "canvas" (avibe/ui's) when absent.
type Shot = { id: string; title: string; status: string; theme: "light" | "dark"; lang?: string; width: number; height: number; interaction?: boolean; hash: string; src: string; canvas?: string }
type Index = { lang: string; shots: Shot[] }

let index: Promise<Index> | null = null
const loadIndex = () => (index ??= fetch(new URL("./design/frames.json", document.baseURI)).then((r) => r.json()))

const STATUS: Record<string, string> = { proposed: "提案", approved: "已定稿", shipped: "已上线" }

type AnnotationApi = { getState(): { enabled: boolean }; subscribe(callback: (state: { enabled: boolean }) => void): () => void }

/** Whether the Show Page's annotation overlay is on, through its public window API. */
function useAnnotating(): boolean {
  const [annotating, setAnnotating] = useState(false)
  useEffect(() => {
    let unsubscribe: (() => void) | undefined
    let timer: ReturnType<typeof setTimeout> | undefined
    let attempts = 0
    // The overlay attaches its API after the page has loaded, so look for it for a while.
    const attach = () => {
      const api = (globalThis as { __AVIBE_SHOW__?: { annotation?: { api?: AnnotationApi } } }).__AVIBE_SHOW__?.annotation?.api
      if (api) {
        setAnnotating(api.getState().enabled)
        unsubscribe = api.subscribe((state) => setAnnotating(state.enabled))
      } else if (attempts++ < 40) {
        timer = setTimeout(attach, 250)
      }
    }
    attach()
    return () => {
      clearTimeout(timer)
      unsubscribe?.()
    }
  }, [])
  return annotating
}

export function DesignFrame({ id, themes = ["light", "dark"] }: { id: string; themes?: ("light" | "dark")[] }) {
  const [data, setData] = useState<Index | null>(null)
  const annotating = useAnnotating()
  useEffect(() => {
    void loadIndex().then(setData)
  }, [])
  const shots = (data?.shots ?? []).filter((s) => s.id === id && themes.includes(s.theme))
  shots.sort((a, b) => themes.indexOf(a.theme) - themes.indexOf(b.theme))
  if (!data) return null
  if (shots.length === 0) return <p className="text-sm text-red-600">设计稿里没有这一帧：{id}（先运行 npm run design:export）</p>
  const first = shots[0]
  return (
    <figure className="flex max-w-full flex-col gap-2" data-frame-id={id}>
      <figcaption className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-semibold">{first.title}</span>
        <span className="rounded border px-1.5 font-mono text-[10px] text-muted-foreground">{STATUS[first.status] ?? first.status}</span>
        <span className="font-mono text-[10px] text-muted-foreground">{id}</span>
      </figcaption>
      <div className="flex max-w-full flex-wrap items-start gap-3">
        {shots.map((shot) => (
          <Live key={shot.theme} shot={shot} lang={data.lang} annotating={annotating} />
        ))}
      </div>
    </figure>
  )
}

/** The frame itself, running the real components; scaled down to fit narrow screens. */
function Live({ shot, lang, annotating }: { shot: Shot; lang: string; annotating: boolean }) {
  const still = annotating || shot.interaction === true
  const box = useRef<HTMLDivElement>(null)
  const [scale, setScale] = useState(1)
  useEffect(() => {
    const parent = box.current?.parentElement
    if (!parent) return
    const fit = () => setScale(Math.min(1, parent.clientWidth / shot.width))
    fit()
    const observer = new ResizeObserver(fit)
    observer.observe(parent)
    return () => observer.disconnect()
  }, [shot.width])
  const src = `./design/${shot.canvas ?? "canvas"}/design.html?frame=${encodeURIComponent(shot.id)}&theme=${shot.theme}&lang=${shot.lang ?? lang}`
  const image = `./design/${shot.src}?v=${shot.hash}`
  return (
    <div ref={box} className="flex flex-col gap-1" style={{ width: shot.width * scale }}>
      <div className="overflow-hidden rounded-lg border shadow-sm" style={{ height: shot.height * scale }}>
        {still ? (
          <img
            src={image}
            alt={`设计帧 ${shot.id}（${shot.title}，${shot.theme === "dark" ? "深色" : "浅色"}）`}
            data-frame-id={shot.id}
            data-theme={shot.theme}
            style={{ width: shot.width * scale, height: shot.height * scale, display: "block" }}
          />
        ) : (
          <iframe
            title={`${shot.title} · ${shot.theme}`}
            src={src}
            loading="lazy"
            data-agent-hidden
            style={{ width: shot.width, height: shot.height, border: 0, transform: `scale(${scale})`, transformOrigin: "0 0" }}
          />
        )}
      </div>
      <div className="flex gap-3 text-[10px] text-muted-foreground">
        <span>{shot.theme === "dark" ? "深色" : "浅色"}{annotating ? " · 批注中，显示截图" : shot.interaction ? " · 截图（点击、悬停或按下时的状态）" : ""}</span>
        <a href={src} target="_blank" rel="noreferrer" className="hover:underline">单独打开</a>
        <a href={image} target="_blank" rel="noreferrer" className="hover:underline">
          截图
        </a>
      </div>
    </div>
  )
}
