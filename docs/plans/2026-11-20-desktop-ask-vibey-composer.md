# Desktop: Ask Vibey puts a selection into Vibey's composer

Owner request: Max, 2026-11-20 (design frames approved 04:20; the w1.9 local package was accepted 09:01). This change carries only the Services → composer path. The earlier D1 draft inbox, Mini draft surface, relay and Runtime receiver stay on reference branches and are not part of master.

## Behaviour
- Select text, a link or files in any app → Services → "Ask Vibey" (zh-Hans 「问 Vibey」).
- The main window comes forward through `focus_or_restore_main_window`. The selection goes into the composer of the main Bot conversation (`ses_main`) on the instance the window shows:
  - text is quoted with `quoteText`;
  - a link is inserted bare;
  - files become composer attachments.
- Each share is appended on its own line. Nothing is sent.
- **Limits:**
  - selection bounds: text 1 MiB, one URL up to 32 KiB, up to 10 files;
  - files over the composer's 100 MiB limit are refused natively, and the page reports them with the existing attachment copy.
- **No current Workbench:** shares wait in memory in order, with one "kept" notice. They are lost only if the app quits first.
- **No Vibey conversation:** if Vibey's conversation can't be opened, or the Workbench has no receiver after the retries, an "unavailable" notice is shown and the share is dropped.
- Design frames: `ui/design/frames/composer/ask-vibey.design.tsx`.

## Contract
- **Native** (`desktop/src-tauri/src/compose.rs`): `eval` into `instance_client::current_workbench`.
  - First it reads `window.__AVIBE_DESKTOP_COMPOSE__.document` (top document, current Runtime origin only).
  - Every later call checks that origin and that document: `begin`, `chunk` (base64, 192 KiB raw), `commit`, then a `status` poll.
  - It needs no Tauri command or capability.
- **Page** (`ui/src/desktop/desktopComposeReceiver.ts`, installed by `DesktopCompose.tsx` in AppShell): it inserts one share at a time through `ComposerBridgeContext.compose`, which ChatPage publishes.
- **Service and bundle:**
  - The selector is `askVibey:userData:error:` (`NSMessage` `askVibey`).
  - Menu titles come from `desktop/src-tauri/macos/{en,zh-Hans}.lproj/ServicesMenu.strings` via `bundle.macOS.files`.
  - `CFBundleLocalizations` is `[en, zh-Hans]`.
- **Files:** `avibe_runtime_host::selection::ordinary_open` / `unchanged`. A file is opened at selection without following a final symlink, then reopened and compared at delivery (device, inode, ctime, length, mtime). A file that differs is refused as unreadable.

## Follow-ups
- **Notice reasons:** several cases end in the "couldn't open Vibey's conversation" notice although the cause differs: voice capture holding the composer, a slow Workbench, an unreadable file, or an outdated remote Workbench. Text over 1 MiB and mixed selections borrow the upload-failed copy.
- **Remote instances** need a Workbench that carries this receiver.
