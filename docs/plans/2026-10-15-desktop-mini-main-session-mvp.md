# Desktop Mini Main Session MVP

## Change Contract

- **Intent:** Make the desktop mini resolve one stable built-in Vibey session through `GET /api/sessions/main`.
- **Backend boundary:** Resolve or recreate `ses_main` under the SQLite writer lock. The row has `scope_id=NULL`, `metadata.kind=main`, the built-in Vibey Agent, and a principal-safe `~/.avibe/main/<principal>/` workdir created on first use. Return `can_chat=true` for an authorized caller even without a Project.
- **Frontend boundary:** `ApiContext.getMainSession()` calls the new endpoint. `PetPage` uses that result to bind the main session on startup and through its Vibey switcher path, while existing bindings continue to validate through `getSessionResult(session_id)`.
- **Compatibility:** Keep `POST /api/desktop-mini/session` unchanged. Preserve existing standalone session behavior by using the new explicit `workdir` only for the main-session path.
- **Validation contract:** Cover first create, reuse, row recreation, non-Show-Page workdir, no-Project creation, and the frontend request path. Run focused pytest, changed-file Ruff, and `ui` build.
- **Out of scope:** Permissions, Inbox, Show Page, IM, native desktop code, context rotation, and migration of old desktop-mini bindings.

## Repair Contract

- Keep the fixed `ses_main` identity and serve only the instance Owner. Do not add
  principal-to-session mapping or editor access for standalone main sessions in this slice.
- Validate every built-in invariant before reusing an existing row. A corrupted row
  returns a coded conflict instead of being silently repaired.
- Treat an archived `ses_main` row as terminal. Normal archive and update paths refuse
  the reserved ID, and the main-session endpoint never reactivates an archived row.
- Prepare the main workdir before reserving the SQLite writer lock. Ordinary standalone
  sessions keep their existing Show Page workdir behavior.
