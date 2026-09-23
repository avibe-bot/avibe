# Source Citation Regression

Run `npm run test:citations` from `ui/` after installing Chromium and WebKit with
`npx playwright install --with-deps chromium webkit`.

The fixture renders the production `Markdown` renderer with a citation sidecar
shaped exactly as the agent-reply bubble passes it, using real CSS, the real
`Popover`, and the shipped i18n bundles. No Avibe service is used and every cited
destination is served by a local route stub. English and Chinese checks run on
desktop Chromium, mobile Chromium, and mobile WebKit.

They cover what a browser has to answer and jsdom cannot: badge height against a
reference `Badge`, three citations in one sentence staying on one line, hover
preview on desktop, first-tap-reveals / second-tap-opens on touch, keyboard focus
revealing the preview and `Enter` opening the source in a new page, the marker
grammar staying literal inside a code example, an unresolved ref staying plain
prose, and the count of requests to the cited domains — zero until the reader
explicitly opens the page, which is how the "no automatic page, favicon, or
metadata fetching" constraint is asserted instead of assumed.

Screenshots and failure traces go under `e2e/.artifacts/citations/`.

The renderer's own matching rules (which links become badges, which stay ordinary
anchors, and how a malformed stored sidecar degrades) are covered in
`src/components/ui/markdown.test.tsx`. Full-service local Incus acceptance and
native-device touch checks remain separate manual validation.
