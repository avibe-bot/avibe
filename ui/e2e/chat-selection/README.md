# Chat Selection Copy Regression

Run `npm run test:chat-selection` from `ui/` after installing Chromium and WebKit
with `npx playwright install --with-deps chromium webkit`.

The fixture renders two transcript bubbles with the production `Markdown`
renderer under the production `SelectionQuoteToolbar`, with real CSS and the
shipped i18n bundles. No Avibe service is used, and the clipboard is a page-local
recorder. Checks run on desktop Chromium and desktop WebKit.

They cover the Ranges only a browser makes: a double-clicked word inside an
emphasis or a code span, a triple-click that ends at the start of the next
paragraph or list item, a drag resolved by hit-testing that starts inside an
emphasis, a drag across two bubbles and the timestamp between them, and Select
all. Each asserts what Copy writes: formatting the selection covers keeps its
Markdown, formatting it cuts is dropped, and nothing unselected is copied.
Screenshots and failure traces go under `e2e/.artifacts/chat-selection/`.

The extraction rules themselves are covered table-driven in
`src/lib/markdownSource.test.tsx`. Touch selection on native devices remains
separate manual validation.
