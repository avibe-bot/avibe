# Desktop deep links (`avibe://`)

## Status

Frozen implementation contract for gap G5. Companion to
`desktop-product-gaps.md` (G5), `desktop-notifications-sse.md` (G4 click
upgrades to a locate, not a redesign), and
`tauri-desktop-vertical-slice.md` (thin shell, Workbench stays the
product UI).

Owner: Avibe core
Date: 2026-09-10
Baseline: `desktop` branch after #1975 (single-instance plugin already
focuses the running window and ignores argv).

## Product outcome

A click on `avibe://session/<id>` (from IM, mail, or a future G4
notification) brings Avibe to the front and shows that session. If the
app was not running, it starts, finishes bootstrap, then shows the
session. Unknown or malformed links are dropped, never echoed back
into a WebView.

v1 is **locate-in-the-existing-window**. Opening a second native
window for a Show Page is G8 and is not this contract.

## Frozen grammar

Scheme: `avibe` (no `+` / no vendor prefix). Registered for the
packaged app identifier `bot.avibe.desktop`.

v1 paths, each a single host-less URL of the form `avibe://<kind>/<id>`
or `avibe://<kind>`:

| Link                              | Workbench navigation                         |
| --------------------------------- | -------------------------------------------- |
| `avibe://session/<session_id>`    | `/chat/<session_id>`                         |
| `avibe://show/<session_id>`       | `/apps/show/<session_id>`                    |
| `avibe://settings`                | `/admin/settings/service`                    |
| `avibe://vaults/request/<request_id>` | `/vaults?request_id=<request_id>`        |

`<session_id>` and `<request_id>` are the product's existing identifiers
(Workbench session ids, vault request ids). They are accepted only when
they match `^[A-Za-z0-9._-]+$`, are at most 128 bytes, and are **not**
the complete path segments `.` or `..`. Ordinary dots inside an id
(`ses.abc`) remain valid.

Rejected complete `.` / `..` because WHATWG URL path normalization
would turn `/chat/.` into `/chat/` and `/chat/..` into `/`, so the
in-window navigation could not land on the mapped session route. Percent-
encoded `%2E` / `%2E%2E` are not those segments and stay subject only
to the character/length rule (they do not normalize as dot-segments).

Rejected (no navigation, no quoted echo into the WebView):

- any scheme other than `avibe`;
- unknown kind / extra path segments / unexpected query or fragment;
- an id that fails the character/length rule;
- anything that looks like a host (`avibe://session@…`, `//` authority).

The mapping table is the contract. A new kind is a contract revision,
not a silent addition in the parser.

## Delivery: the shell never talks IPC to the Workbench

Two launch shapes, one Workbench mechanism.

1. **Hot path** (app already running). `tauri-plugin-single-instance`
   already hands a second launch to the live process and focuses the
   main window (`desktop/src-tauri/src/lib.rs`). v1 extends that
   callback to read argv, parse one `avibe://` URL, and — after the
   window is showing the Workbench origin — ask the WebView to
   navigate **in-window** to `{origin}{path}{query}` from the table
   above. Same origin as the currently adopted Runtime; never a
   different host.
2. **Cold path** (app was not running). OS starts the packaged app
   with the URL in argv. The shell stashes the parsed target, runs
   the existing bootstrap/adopt flow unchanged, and on the first
   successful Workbench navigation includes the target path so the
   SPA lands on it instead of the default session list.

Both paths resolve to "the Workbench SPA navigates to a same-origin
URL it already implements". There is **no new Tauri command, no new
capability, no `remote` grant**. A Workbench page still cannot invoke
bootstrap IPC; the shell pushing a same-origin path into its own
WebView is not an IPC grant.

If the Runtime is not yet ready (bootstrap screen still up), the
stashed target waits. Bootstrap failure discards it and stays on the
bootstrap screen — a deep link must not override a failed Runtime
with a half-navigated Workbench.

## Why not an IPC command

An IPC `open_target` callable from the WebView would have to be
granted to the navigated Workbench to be useful on the hot path,
which is exactly the capability boundary `shell_boundaries.rs`
forbids. Same-origin in-window navigation keeps the privilege in
the shell: only the process that owns the window decides to move it.

## Producer side (Python, separate from the shell PR if needed)

Today `vault_request_url` / IM formatters emit public `https://`
Workbench URLs via `workbench_url` (`core/avibe_cloud.py`). That
remains the right link for browsers and for users without the
desktop app.

v1 desktop does **not** require those producers to switch. The
shell's job is to honour `avibe://` when the OS delivers one.
Producers may start emitting `avibe://` **in addition to** https
(for example in a native-notification payload once G4 lands) under
their own PR; that is a link-grammar consumer, not a scheme-parser
change. Until they do, G5 is still testable: open
`avibe://session/<known-id>` from Terminal / a test page.

Do not replace https links in IM with `avibe://` in v1: IM clients
are not the desktop OS and cannot route the scheme.

## Plugin and packaging

- `tauri-plugin-deep-link` registers the scheme at build/install
  time for the packaged app. Development `tauri dev` may not receive
  OS-level `avibe://` clicks (scheme registration is an installed-app
  concern); argv injection and the single-instance callback remain
  the testable path.
- macOS: the scheme lands in `Info.plist` via the plugin. That file
  is generated; do not hand-edit it in source.
- Windows: the scheme lands in the NSIS file association. Unsigned
  acceptance builds still register it for that install; signing (G1)
  does not change the grammar.

## Tests (invariants)

- Parser: every row in the mapping table produces the exact
  Workbench path; every rejected shape (wrong scheme, extra
  segment, bad id, complete `.` / `..` path id, authority) produces
  `None` and never includes the raw input in a UI string. An id that
  merely contains a dot still maps.
- Hot path: a second-instance argv containing a valid link focuses
  the main window and requests in-window navigation to the mapped
  path on the **currently adopted origin**, not on `devUrl` / port
  1420.
- Cold path: a stashed valid target is applied on the first
  successful Workbench navigation and then cleared; a failed
  bootstrap leaves the stash dropped and the window on bootstrap.
- Boundary: no new command appears in `src-tauri/build.rs`;
  `shell_boundaries.rs` still forbids `remote` grants. Deep-link
  handling lives in the single-instance callback / argv path, which
  is not WebView-callable.
- Capability: a Workbench origin (remote after navigation) still
  cannot invoke bootstrap commands after a deep link is consumed.

Drive parser tests without Tauri (pure function in `runtime-host` or
an equivalent Tauri-free module). Drive the stash/apply state
machine with the existing fake-Runtime bootstrap tests.

## Explicit non-goals (v1)

- Native multi-window as the link's destination (G8).
  `avibe://show/<id>` in v1 locates inside the main window.
- Replacing https Workbench URLs in IM / mail.
- Authenticating the link (ids are capabilities-by-guess of a
  local-only app; loopback Workbench already assumes the user on
  the machine).
- Query parameters other than `request_id` on `/vaults`.
- Custom URL scheme beyond `avibe`.

## Sequencing

- Independent of G1 signing. Scheme registration works on unsigned
  acceptance builds.
- G4 may adopt these URLs as notification click targets in a
  follow-up; G4 v1 remains "focus the window".
- G6 (window state) is independent; a restored frame still receives
  the in-window navigation.
- G8, when it exists, may reinterpret `avibe://show/<id>` as "tear
  out that Show Page"; the grammar stays, the destination policy
  is a G8 revision of this table's third column.
