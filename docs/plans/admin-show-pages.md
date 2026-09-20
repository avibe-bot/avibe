# Admin Show Pages management page (superseded)

This historical plan was implemented before Show Pages moved into the Workbench
App Library. Its separate `Private / Public / Offline` visibility control,
per-action public-link mutations, and dedicated Admin navigation are retired.

The current design is documented in
[`show-access-local-settings.md`](show-access-local-settings.md):

- audience is applied atomically as `Private / Limited / Fully public`;
- availability is an independent online/offline control;
- the App Library expanded row and the Show Page Share popover reuse the same
  access editor;
- legacy Web visibility, link-rotation, custom-link, and hosted-email mutation
  surfaces must not be restored.
