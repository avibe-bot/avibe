# Badge Trigger Regression

Run `npm run test:badge-triggers` from `ui/` after installing Chromium and WebKit
with `npx playwright install --with-deps chromium webkit`.

The fixture renders the production `VersionBadge` and `BackendRuntimeCard` with
real CSS, mocked API responses, and local toggle state. No Avibe service is used.
English and Chinese checks run on desktop Chromium, mobile Chromium, and mobile
WebKit. They cover compact version and lifecycle visuals (ready, update available,
disabled, missing, and unchecked), taps near both edges of the 44px mobile target,
desktop hit bounds, keyboard activation, and the adjacent switch and path input.
Screenshots and failure traces go under `e2e/.artifacts/badge-triggers/`.

The audit found that `VersionBadge` and `BackendLifecycleChip` are the only
consumers of the mobile badge trigger recipe. Full-row settings navigation,
primary actions, and icon tiles intentionally retain their larger dimensions.
The fixture proves browser layout and pointer routing; full-service local Incus
acceptance and native-device touch checks remain separate manual validation.
