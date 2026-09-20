# Issue #1412: Settings in the Workbench shell (phases 1–2)

## Goal

Make Settings a routed page inside the persistent Workbench shell. Replace the
admin sidebar and settings tab bar with one responsive section rail while
preserving old deep links.

## Scope

- Mount the existing settings surfaces under canonical `/settings/*` routes.
- Add Preferences pages and a four-group Settings rail.
- Redirect every retired `/admin/*` destination to its canonical replacement.
- Merge Diagnostics and Logs into one rail destination.
- Keep the Workbench sidebar, Apps launcher, appearance controls, and account
  controls available while Settings is open.
- Retire the Dashboard destination; Service and Platforms remain the canonical
  homes for its runtime and connection information.

## Deferred

- Capability-safe navigation projection and locked/read-only placeholders.
- Platform-owned group and DM detail views.
- Backend provider detail redesign.
- Rail health/error badges.

## Validation

- Route and layout unit tests cover canonical and legacy navigation.
- Existing settings tests remain green.
- `npm run build` verifies the production UI bundle.
