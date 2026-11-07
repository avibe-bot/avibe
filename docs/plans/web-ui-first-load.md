# Web UI First Load · Plan

> **Status**: Implemented in the change that adds this file (2026-11-07).
> **Owner**: cyhhao

## 1. Background

The Web UI is opened most often on an iPhone over the remote tunnel, and a
release ships almost every day. Measured on master before this change (RC17
build, section 6):

- `index.html` named 35 scripts and one stylesheet. The entry chunk alone was
  3.3 MB raw / 960 KiB gzip, and the first screen downloaded 1.2 MiB gzip of
  JavaScript and parsed 4.2 MB of it before it drew anything.
- Every route, Settings, the setup wizard, both locales, the Lexical editor, the
  Markdown pipeline and the Vault cryptography were in that entry, so they
  loaded on every first visit whatever the page showed.
- Assets are content-hashed and cached `immutable`, but any app change renamed
  the entry, so the first load after each release downloaded the whole entry
  again (1 MB gzip).
- The in-app logo was an 800×800 PNG (488 KB) drawn at 24–44 px.

## 2. Goal

The first screen (the home, or a conversation) downloads and runs only what it
draws, on a first visit and after a release, with no visual or interaction
change: the local message cache, offline reading, the iOS shell, voice and push
behave as before. A CI budget keeps it that way.

## 3. What loads when

- **Entry** (`index.html`): the app shell (providers, AppShell, the home and
  Search) in `index-*.js`, and the libraries it imports in `vendor-*.js`
  (`vite.config.ts`, chunk group on `$initial` modules). The vendor file changes
  only with a dependency update, so it stays cached across releases.
- **Locale**: one language chunk (`src/i18n/app.ts`, an i18next backend), the
  one the detector picks. `index.html` starts its download with the entry
  (`ui/scripts/firstScreenPreload.ts` repeats the detection: the stored pick,
  then the browser's first language); the first render waits for it
  (`src/main.tsx`). A later switch keeps the current language on screen until
  the other one's copy has arrived. Chinese reads no English fallback, so a
  Chinese page never downloads the English copy: both locales carry the same
  keys, which `src/i18n/keyCoverage.test.ts` checks per locale with
  `fallbackLng: false`. The design canvas, the bundled mobile onboarding and the
  tests keep the synchronous two-language instance (`src/i18n/index.ts`).
- **Routed pages** (`src/routePages.ts`, `src/lib/lazyPage.ts`): Chat, Inbox,
  Projects, Agents, Skills, Harness, Vaults, the setup wizard, and all of
  Settings (one chunk). The page the app opens on starts loading with the entry
  (for a conversation, `index.html` preloads it; an installed iPhone app that
  restores a conversation counts as one). Once the shell is up, the other pages
  load one at a time in idle time, so later navigation does not wait, and a
  page can still be opened after the connection drops. A navigation that does
  reach an unloaded page is a transition: the current screen stays until the
  page is ready. A page whose download fails shows the existing error card,
  whose reload recovers it.
- **Deferred parts of the shell**: the chat composer's Lexical editor loads with
  the chat page (`deferredMentionEditor.ts`); the sidebar Inbox peek's Markdown
  preview and an editor's preview tab load the Markdown pipeline (and with it
  the Vault cryptography) on demand.
- **Libraries of lazy chunks** go into `lib-*` chunks grouped by which pages use
  them, so a release that changes only app code leaves Lexical, Markdown,
  Monaco, xterm and pdf.js cached.
- `src/assets/logo.png` is 256×256 (48 KB), enough for its largest use at 3×.

A stylesheet imported by a lazy module now loads with that module's chunk. The
Model Hub classes are drawn outside the Model Hub page, so each module that draws
them imports `modelHubSurface.css` itself.

## 4. Budget

`ui/scripts/check-first-load-budget.mjs` runs after the UI build in the required
`build-linux-artifacts` job (`npm run check:first-load`). It measures, gzip level
6 as the UI server sends it, the JavaScript `index.html` loads plus the larger
locale (home), and that plus a conversation's preloads (chat). Budgets: 600 KiB
home, 900 KiB chat. Raise one only in the change that needs it, with the reason;
first move what the first screen does not draw off it.

## 5. Brotli

Not in this change. Precompressed at quality 11, the split build's first screen
is 17–21% smaller than at gzip level 6, and in the measurement of section 6 it
was 0.06–0.14 s faster still (home 1.19 → 1.08 s cold, 0.79 → 0.70 s after a
release; a conversation 1.96 → 1.82 s and 1.18 → 1.12 s). Serving it needs a
`.br` beside each asset (about 5 MB more in the package, and about 25 s more
build time at quality 11), an `Accept-Encoding` branch in the UI server's static
response, and Cloudflare Tunnel passing the origin's encoding through to the
browser, which is to be confirmed on a real tunnel. It is a follow-up, measured
on its own.

## 6. Measurement

Playwright WebKit, iPhone 13 profile, against a local stand-in for the tunnel:
HTTP/2 over TLS, 170 ms RTT, one shared 9 Mbps downlink, gzip level 6 and the UI
server's cache headers, API reads replayed from a recording. "After a release"
loads the previous build (home and a conversation, idle prefetch finished, so
the HTTP cache and local message cache are warm), then serves a build with the
rc15→rc16 change set applied and opens the page again. Times are medians of
three; bytes are what the server sent before the page was usable (a composer
on the home; a composer and messages on a conversation).

| Scenario | Page | Usable | First paint | Bytes before usable | Requests | JS parsed |
| --- | --- | --- | --- | --- | --- | --- |
| First visit, before | Home | 2.23 s | 1.60 s | 1,805 KB | 37 | 4.16 MB |
| First visit, after | Home | 1.19 s | 0.95 s | 659 KB | 6 | 1.81 MB |
| First visit, before | Conversation | 2.72 s | 1.61 s | 1,873 KB | 37 | 4.16 MB |
| First visit, after | Conversation | 1.95 s | 1.23 s | 1,016 KB | 46 | 2.72 MB |
| After a release, before | Home | 1.42 s | 1.42 s | 1,044 KB | 3 | 4.16 MB |
| After a release, after | Home | 0.77 s | 0.77 s | 375 KB | 3 | 1.81 MB |
| After a release, before | Conversation | 1.54 s | 1.41 s | 1,044 KB | 3 | 4.16 MB |
| After a release, after | Conversation | 1.14 s | 1.15 s | 491 KB | 13 | 2.72 MB |

Bytes include the API reads made before the page was usable. A conversation
after a release downloads the shell, its locale and its page chunks, which
change with any app change; the libraries (`vendor`, `lib-*`) stay cached.

## 7. Follow-ups

- The app's own chunks still change names whenever the entry does (each imports
  it by its hashed name). Import maps (`build.chunkImportMap`, experimental in
  Vite 8) would end that cascade; at the time it saved little and was left out.
- The Google Fonts stylesheet blocks the first script; self-hosting the fonts
  would remove a third-party connection from the first load.
- Desktop-only shell parts (the sidebar, window layer) are still in the entry a
  phone loads.
