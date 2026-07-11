# Issue 803: Router-wide unsaved changes guard

## Background

The full-page Editor introduced in #800 keeps unsaved buffers inside the routed page. Its hard-unload
and window-close guards do not protect desktop links, imperative SPA navigation, or browser Back.
The interim `NavGuardContext` covers only selected mobile controls because `BrowserRouter` cannot use
React Router's supported `useBlocker` API.

## Goal

Give every route-mounted dirty surface one reusable registration contract while keeping exactly one
router blocker. Preserve hard-unload protection and keep windowed-app close behavior independent.

## Design

- Replace the root `BrowserRouter` with `createBrowserRouter` and `RouterProvider`, retaining the
  current pathless auth/AppShell layout and all child routes.
- Mount `UnsavedChangesProvider` as the data router's root layout. It owns the only `useBlocker` and a
  stable-identity message registry.
- Aggregate both full-page Editor variants' dirty state in `AppsEditorPage`, then register once and
  retain the existing `beforeunload` listener.
- Remove manual AppShell confirmation wrappers. Links, imperative navigation, and POP transitions all
  pass through the central blocker; window close guards remain unchanged.

## Verification

- [x] Registry unit tests
- [x] Changed-file lint and UI typecheck/build
- [x] Desktop sidebar cancel/confirm with buffer preservation
- [x] Browser Back cancel, retry, and confirm
- [x] Mobile bottom navigation and Back
- [x] Imperative navigation, clean navigation, and hard-unload listener
- [x] Setup/auth/remote redirect, lazy Apps routes, and legacy redirects
- [x] Local Incus worktree service and mobile blocker smoke

The repository-wide ESLint command still reports the existing baseline (333 errors and 42 warnings,
including unchanged code in `App.tsx` and `AppShell.tsx`). The new modules pass ESLint, and the
changed existing files pass after suppressing only their baseline rule categories.
