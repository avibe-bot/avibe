# Changing this Avibe's address, and cleaning up after the old one

Status: rule table agreed with the independent reviewer (November 21, second pass); implemented, awaiting the backend half for the exercise. The settings page that calls these routes is separate work. Base `master` `ca534deea`
(#4107, migration 0096; this work takes 0097). Backend
half: avibe-bot-backend `docs/plans/personal-hostname-release.md` (candidate
`f7324025`, "Backend ↔ instance wire"). What was settled with that writer is
listed under [Contract points to settle](#contract-points-to-settle).

## Background

A personal Avibe is reached at `<prefix>-app.avibe.bot`. The Owner can change
the prefix from this instance's settings. The old name is not kept for ever:
after a waiting period (one number set by avibe.bot, 30 days today) other
people can claim it.

A browser keeps data per address: two IndexedDB databases with conversations
and attachment names, drafts and other localStorage entries, Cache Storage, the
HTTP cache (some authenticated responses are `private, immutable` for up to a
year), the `/push-sw.js` service worker and its push subscription, and the
sign-in cookie, which is readable JSON naming the visitor. Whoever holds the old
name later can serve a page there and read whatever an earlier visitor's
browser still holds. A phone may also keep sending Health data and its
credential to the old address. So everything this instance can reach is emptied
before the name can pass on, and the Owner is told what could not be reached.

During the waiting period avibe.bot keeps the old name routed to this same
computer. This instance answers it with cleaning only.

## Terms

For every request the instance gives the request's Host one **role**. A
**managed name** is a Host matching the managed grammar (`<prefix>-app.avibe.bot`,
the pattern the direct lifecycle already uses).

- **active**: for a managed name, exactly the Host of
  `remote_access.vibe_cloud.public_url` as recorded on this computer, and no
  other. An instance paired before this change starts with the name in its
  pairing record; it does not need a state read before serving. For a name
  outside the managed grammar (an organization's custom hostname), membership
  in the existing active-hostname snapshot, as today. The snapshot never makes
  a managed name active (B7).
- **cleanup**: a managed name other than the active one that a fresh state
  from avibe.bot lists as a cleanup hostname of this instance. Cleaning only.
- **quiet**: a cleanup name on its way to becoming active, from before the
  request (or from the state that announces it) until the switch. One fixed
  answer with no content and no clearing.
- **former or foreign**: any other managed name. The same fixed answer with no
  content and no clearing, on every path (E8).
- **unknown**: a Host outside the managed grammar that is not active. Today's
  behaviour, unchanged.

Hosts are compared with the one existing normalizer (lower case, port and
trailing dot removed). Content is refused when either the request's own Host
header or a trusted forwarded Host is a managed name that is not active.
Cleaning is decided on the request's own Host header only, never a forwarded
one. Local addresses can never be cleanup names: only a managed name that
avibe.bot listed for this instance id can be.

## Rule table

### A. Who may ask

| ID | Who could do what | What would be lost | Rule | Acceptance check |
|---|---|---|---|---|
| A1 | A member, a viewer, another account's device, a demoted Owner with an old cookie, a forwarded or LAN setup-host request, or a cross-site page calls the rename routes. | Every session ended, the name given away after the waiting period, Health data deleted. | The three routes need the instance Owner by **current** authorization (not the role at sign-in), or a direct loopback connection on a paired instance. Same-origin and CSRF checks as every mutation. Never the LAN setup host, a trusted-proxy peer or the Docker probe. Refusal is `403 owner_required` and has no side effect. | Member, viewer, demoted Owner, second account's Owner cookie from another instance, setup-LAN, forwarded loopback, missing Origin, wrong CSRF: each refused, journal empty, no request to avibe.bot. Remote Owner and direct loopback succeed. |
| A2 | One scripted or replayed request, a stale tab, a double click, two tabs racing. | A rename nobody confirmed, or two operations in flight. | Two requests: `preview` returns a `confirmation_id` (random, 10 minutes, single use) bound to the caller's session (or loopback), the prefix, and the address generation it saw; `change` takes only that id. One operation in flight per instance; a second gets `409 operation_in_progress`. The page's two confirmations happen between the two requests; the server cannot see clicks and does not claim to. | `change` without preview, with another session's id, after expiry, after reuse, after the address changed: refused. Two concurrent `change` calls: one journal entry, one request to avibe.bot. |
| A3 | An organization instance, an unpaired instance, or one with remote access off asks. | Nothing; it must not reach avibe.bot. | Refused locally with `409 rename_ineligible` when `instance_kind` is not `personal`, when the pairing cannot reach avibe.bot, or when the active name is not a managed name (an instance bound to a control plane on another domain never enters the role logic from this side). avibe.bot refuses too. | Organization, unknown kind, unpaired, active name outside the managed grammar: refused with no outbound request. |
| A4 | A process on this computer (an Agent, the CLI) calls the loopback route. | As A1. | Accepted: local code execution on the Owner's computer is the Owner today (it can read the device secret, revoke Health with the CLI, unpair). Stated, not narrowed. | None beyond A1; recorded as a known boundary. |
| A5 | Someone holding a copy of the device secret renames at avibe.bot directly, without this instance. | With a naive instance: synced Health data deleted by a person who never touched the machine (the phone restores only 30 days). | Destruction follows only a rename **this instance wrote down before asking**: the operation id is in the local journal and the committed receipt matches it. When avibe.bot reports a name change that matches no journal entry, the instance adopts the reported roles (so the Owner can reach it, and the old name is cleaned like any cleanup name: visitors lose a cache they can load again), ends all remote sessions with the same call as C1 (which arms no Health owner check), refuses Health uploads on the old name, **deletes nothing**, and shows the Owner "the address was changed outside this Avibe: bind remote access again, then take the name back" (`unrecognized_change` in the state route). | Inject a state with a new active name and an operation id unknown to the journal: Health pairing rows and synced days still present, sessions ended, old name cleaning, `unrecognized_change` set. Same with an operation id that is in the journal but for a different target or generation. No Health owner check is queued. A return announced by the state (a cleanup name becoming active without a journal entry) goes through the same quiet, drain and settle as B3. |
| A6 | The Owner binds remote access again (new device secret, same instance id) and takes the old name back. | The promised recovery path in the notice email. | The journal, the roles, the clearing list and its receipts are keyed by instance id; none of them depends on the device secret, on the registry binding (a re-bind mints a new cookie-signing secret and starts a new registry), or on verifying an old cookie's signature. After re-binding, the next state read under the new secret lists the same cleanup names, and the rename form can take any of them back through the same route. Taking back is never limited locally. | Re-pair the same instance id with a new secret, read state, take back the old name: succeeds; the old secret's replay of a pending operation is refused by avibe.bot and changes nothing locally. A browser that held data under the old binding still gets its receipt embedded on the old address and can mark its row. |

### B. The switch, crashes and order

| ID | Who could do what | What would be lost | Rule | Acceptance check |
|---|---|---|---|---|
| B1 | A crash or lost reply between avibe.bot accepting the rename and this instance switching. | The instance believes the old name; visitors sent to the new name find nothing. | Before the first request the instance writes the intent to a crash-safe journal: operation id, expected generation, expected hostname, new prefix, who confirmed. It sends the identical tuple until it gets a final answer (`202` means repeat the same request after `Retry-After`). On a committed receipt it records the receipt, reads the hostname state, then applies in this order, each step idempotent and recorded: (0) the device list and its receipts are snapshotted for the name about to retire, so that a page that clears and reports the moment it is told is not reporting to nothing; and, for a change of its own, the time is written into the journal as the Health cut-off, so that a phone paired at the new address later is never this change's to delete (no Health store is opened here: an unreadable store must not hold up the switch); (1) the role flip: roles and config (`public_url`, `redirect_uri`, OIDC client id) in one config write, the active-hostname snapshot rewritten without managed names (B7), the connector's recorded signature updated under its lifecycle lock so that a later Start or reconcile does not bounce the tunnel, and **open pages woken at once** (D1); (2) one database transaction: snapshot the device list, end all remote sessions, disable remote push subscriptions; (3) after a short grace for the final frames, every stream and socket still open on the old Host is closed (C1); (4) Health (C4). On start and on every state read an unfinished journal entry resumes from its recorded step. | Kill the process after each step and after a lost `200`: on restart the same tuple is replayed, the end state is identical, nothing runs twice (Health deletion in particular), and no second operation id is minted. A refusal clears the journal and changes nothing. |
| B2 | A stale or wrong view makes the instance send cleaning responses on the name it is serving. | Every visitor's cache and drafts wiped and everyone signed out, for as long as it lasts. | One local role table decides content and cleaning, read once per request. (a) The locally recorded active name is never cleaned, whatever any list says; a name outside the managed grammar is never cleaned at all. (b) A cleaning response needs the Host to be in the last cleanup list avibe.bot gave for this instance id, at a generation not older than the stored one, not past the end date avibe.bot gave for it (the instance never computes 30 days itself), and from a state read that was **started** no more than 60 seconds ago by both the monotonic clock and the wall clock (the monotonic clock stops while the computer sleeps; a wall-clock difference that is negative or over 60 seconds also ends the permission); this is checked again immediately before the response headers are written, without waiting: while the role lock is busy the check is asked again a few times and then answers no (that visit gets the plain `503` and is cleaned on its next request). A state read is applied only if, at that moment, no `401` has dropped authority since the read started, the pairing is the same and no newer generation is recorded; this holds at every point a read is used, including after the settle and after the config write of a switch. Neither the name being left nor the name being taken is cleaned while the address moves. Otherwise the instance reads the state first (one read at a time per instance) and, if it cannot, answers the unknown-Host `503` with no clearing header and no script. A restart, a `401` from the state read, a pairing write, or a sleep or suspension past the 60 seconds drops the permission; a late or older reply cannot restore it. (c) The instance never infers **active** or **cleanup** from a suffix, a timeout or its own journal alone (refusing content by a name's shape is E8). | Property test over role tables: no Host is both served and cleaned. For every route class (page, API, static asset, `/api/session`, `/auth/*`, `/p/*`, device route, WebSocket) on the active Host with that Host also injected into the cleanup list: content, never a clearing header. State unreachable, or read started more than 60 s ago (measured from the start of the read; simulated sleep where only the wall clock advances): `503`, no header. Spellings of the Host (upper case, trailing dot, port) decide the same way; a forwarded Host never causes cleaning. After restart with no fresh read: `503`. Past the listed end date: `503`. A reply that arrives after a `401` does not re-enable cleaning. |
| B3 | Any change of a name from cleanup to active (asked for here, or made elsewhere and learned from the state): a cleaning response produced a moment earlier reaches a browser after the name is active again. | That browser's fresh sign-in and cache. | Before sending a request whose target is one of its own cleanup names, the instance moves that name to **quiet**, waits until cleaning responses already started on it are fully written, and records this in the journal. avibe.bot does its half for a return from any requester: it stops listing the name for cleaning, raises the generation, and does not publish the name as active until every 60-second permission it handed out has run out (it answers `202` meanwhile; the instance replays). The name becomes active here only after the commit, a state read that names it active, and the local drain; a short settle is added but is not treated as proof that a browser has finished with an earlier response. A refusal returns it to cleanup only after a fresh state read still lists it. What cannot be promised by either side: recalling a response already on the network. | Hold a slow cleaning response open, take the name back: the name is not served until the response finished and the settle passed; no clearing header is produced for it after the journal says quiet. Refused take-back: name cleans again only after a fresh state lists it. |
| B4 | Two renames in a row (A→B→C), then back (C→A). | A browser returning on any of the three ends in the wrong place. | Roles come from the state: after A→B→C, C is active and A and B are cleanup, each with its own `release_at`. After C→A, A is active (B3), B and C are cleanup, and A's clearing list is closed at the switch (F4). A returning browser on a cleanup name is cleaned and told the Avibe moved; on the active name it is served and its old cache is wiped by the existing ended-session rule before sign-in. | Walk the three renames with one browser profile per name: each lands as stated; no profile is cleaned on the active name. |
| B5 | Replies arrive out of order (overlapping heartbeats, a replayed receipt). | A newer role table overwritten by an older one. | The stored generation only grows; a state with a lower generation is dropped. A replayed receipt changes no role by itself: the state is read first. Active and cleanup overlapping in one state, or a malformed list, is treated as unreadable (B2b). | Deliver generation n+1 then n: roles stay at n+1. Overlap in one state: no content change, no clearing. |
| B6 | avibe.bot withdraws a cleanup route: the waiting period ended, or the live instance is transferred to another account. | Requests of the former holder's visitors answered by a machine that should no longer answer for that name. | A name that is no longer in the cleanup list gets nothing from this instance: no cleaning and no content, the fixed answer of E8 on every path. Its clearing list is closed (F4). | Remove a name from the state: next request on it, including `/api/session` and `/assets/*`, is the fixed `503` without clearing header; the device list no longer shows it as pending. |

| B7 | The active-hostname snapshot names the old managed name: it is whatever the last heartbeat returned (avibe.bot's routable set, which includes managed names), kept without expiry. | Content on the old name, and no cleaning there, until the next heartbeat or for the whole waiting period. | A managed name is active only by the local record. The snapshot is filtered on read and on write to names outside the managed grammar, and is rewritten in the same step as the role flip. No list can make a managed name active or keep it from being cleaned. | After the switch with a stale snapshot that names the old managed name, and again after a heartbeat reply that lists it: the old name is cleaned, never served. A custom hostname in the snapshot is still served. |
| B8 | Something other than the journal writes `public_url`: a generic settings save by an Owner (it deep-merges the payload), or a pairing. | A stale client or script writes a cleanup name back: content on a name in its waiting period, never cleaned. | The generic save refuses any new value for `public_url`, `redirect_uri` and `client_id` that differs from the stored one, also from empty (clearing them, which is how the pairing is turned off, stays possible), as it refuses the connector controls today. A pairing write drops every cleaning permission, the journal of another instance id and the quiet state; roles are rebuilt from a state read before anything is cleaned. | Owner settings save that changes any of the three fields: refused, config unchanged. Re-pair to the same and to a different instance id: no cleaning until a fresh state is read; a different instance id inherits no cleanup name, journal or clearing list. |

### C. What ends at the switch

| ID | Who could do what | What would be lost | Rule | Acceptance check |
|---|---|---|---|---|
| C1 | A signed-in browser, the iPhone App's WebView, or a copied cookie keeps working after the rename. | "Every device signed out" would be false. | All remote sessions end through the registry's existing all-remote sign-out in its **host form** (no caller, no subject, also when a remote Owner asked: the caller form would end only that account's other sessions): a tombstone per registered session and the instance-wide cutoff for unregistered old cookies. The call arms neither of the sign-out's deferred effects: no account sign-out delegation and no Health owner check (that check ends in a purge). The cookie-signing secret is **kept** (see contradiction 1). Every stream and socket open on the old Host is closed at the switch, including those admitted without a session (a public Show page's event stream and its reload socket); sign-in flows started on the old Host cannot finish there. **avibe.bot account sessions are not ended:** since #4107 a device sign-out can also ask avibe.bot to end the account sign-in; the rename calls the sign-out without that delegation, so nothing is queued for avibe.bot and people can still sign in there to find the new address. Account sign-outs already queued by earlier device sign-outs are untouched (their pairing does not change). | A cookie from before the switch, replayed by hand on the new Host: `401`. An unregistered pre-upgrade cookie: `401`. A fresh sign-in on the new Host works. A workbench stream, a public Show page's stream and its socket on the old Host are all closed within seconds of the switch. No Health owner check and no account sign-out work is queued. Activity history from before the rename is still listed. After a rename the account sign-out queue holds no new work and each receipt says local only. |
| C2 | The instance keeps pushing to a browser subscription created under the old address. | After the name passes on, the next holder's service worker receives this instance's notification text. | In the same transaction that ends the sessions, every Web Push subscription registered through the remote address (`user_key` `remote:*`, attested or not) is disabled and cannot be re-enabled by a rotation or re-registration arriving on a cleanup Host. Subscriptions made on this computer's local address are untouched. Native App registrations follow the existing sign-out rule and come back when the App signs in at the new address. | After the switch no send is attempted to a pre-rename remote subscription, including an unattributed legacy row; a `pushsubscriptionchange` post on the old Host changes nothing; a local-origin subscription still delivers. |
| C3 | The direct transport keeps answering for the old name, or has no certificate for the new one. | A second door to the old name; direct path dead after rename. | No change to the certificate flow; confirmed by the direct lane (November 21): `public_url` is part of the lifecycle's config signature, so the config write withdraws the rendezvous, stops the sidecar and listener (closing live transports) and voids the old permission; the next certificate poll discards the old name's key and files and orders the new name's certificate. A direct request whose Host is not the active name keeps today's `421`; cleaning responses are not sent on the direct listener. **Order that must hold:** avibe.bot's name changes before this instance's `public_url` does, or the new order is refused and retried for up to an hour; B1 applies the config only after the committed receipt, so it holds. | Test here: the config is never written before a committed receipt; a direct-ingress request on a cleanup Host is `421` with no clearing header. Direct path after a rename is part of the exercise. |
| C4 | The phone keeps syncing Health to the old address; synced data stays after the Owner was told it is deleted; or data is deleted for a rename the Owner did not make. | Health data and the device credential reaching a later holder; or data lost to an impostor. | For a rename from this instance's own journal (A5), after the switch: revoke every Health pairing made before the journal's cut-off with the existing revoke and delete its synced days; a pairing made after the cut-off is never touched, however often the step repeats. If the private store cannot finish, the durable cleanup marker retries and the state route says `health: pending`, never `deleted`. For any other name change: no revoke-and-purge. In both cases Health device routes on a cleanup Host get `401 device_unauthorized` without the body being read (E5). The preview states that Health data will be deleted and the iPhone connected again. | Own rename: pairing rows gone, synced days gone, old credential refused on the new Host too. Crash between switch and Health: completed on restart, once. Phone paired after the rename, then a replay of the old receipt: its data stays. Foreign change: data intact. Canary values never in logs. |
| C5 | Share links and Show pages already handed out. | People holding an old link. | Share records and Show pages are kept and work at the new address under the same paths; links carrying the old name are dead (E1), including those this Avibe already sent into chats and mail. Nothing is redirected. The preview says so. Share guests are not devices and appear in no list (E2 for their cookie). | Old-name `/p/<id>`: cleaning response. Same path on the new name: served to an entitled reader. |

### D. Open pages

| ID | Who could do what | What would be lost | Rule | Acceptance check |
|---|---|---|---|---|
| D1 | A page open on the old address when the rename takes effect; and the question of who is told the new address. | Its data stays until that browser happens to return. | The wake is part of the role flip (B1 step 1), before the sessions are ended. Each event stream checks its Host's role **before** its session; a stream on a Host that has become a cleanup name gets one final frame, produced by the stream itself for that connection: `instance.address-retired` with `moved_to` (the new origin) and that session's clearing receipt (F2), then the stream ends. **Only these connections are told the new address**: they held a verified, currently authorized session at the moment of the switch. Nobody else is told by this instance; they find it in their instance list at avibe.bot, which checks who they are. | Two members and the Owner with open pages: each gets its own receipt and the new origin. A stream opened after the switch on the old Host gets the cleaning response and no address. A stream that wakes for an ordinary event between the flip and the sign-out sends the final frame, never the signed-out frame. |
| D2 | The page leaves for the new address without having emptied the old one, or clearing is skipped by blocking a request. | Data left under the old address by a browser that will not return there. | On the frame the page clears by itself, needing no request to succeed: both IndexedDB databases, all localStorage (leaving only the write fence: the three counters other tabs check before writing, at a value no tab holds, and a marker; see D6) and sessionStorage, Cache Storage, the push subscription (unsubscribe), every service worker registration. Script cannot reach the HTTP cache (authenticated responses can sit there for a year), so the page then makes one credentialed request to the old address, which carries its receipt and is answered with `Clear-Site-Data: "cache"` only (storage again would take the write fence). Only when that answer arrived does it go to `moved_to`, with the current path only. If any clearing step fails or does not finish in 5 seconds, or that request gets no answer, it goes to the **old** address's root instead, where the response to the navigation does the clearing. Sign-out's cross-tab fence makes other tabs follow. | Browser test in the repository's safe browser (both clipboard paths stubbed, no keychain, no window focus): seeded origin is empty after the frame, including drafts, recent files, layout, notification device id, caches, registration, subscription. With that request blocked: storage still empty, and the page lands on the old root, not the new address. In Chromium the answer to that request empties the HTTP cache and leaves what the page wrote. With IndexedDB deletion blocked: lands on the old root, no receipt. |
| D3 | A tab that was suspended and missed the frame. | As D1. | Every response on a cleanup Host carries `X-Avibe-Address: retired`. The page's fetch layer and the session probe treat that header as the frame without `moved_to`; a page whose event stream is still open waits up to 3 seconds for the final frame first, so that a request racing the frame does not cost it the new address. Then: clear as in D2, post the receipt it got when its stream connected, go to the old root. | Suspend, rename, resume: the first response clears the tab; receipt posted; lands on the cleaning page. |
| D4 | A member or a forged event makes someone else's browser clear itself. | Another person's cache and drafts. | The page acts only on the stream's own final frame or the response header. Neither can be produced by a broker publisher or a route: a published event of that name is dropped by the stream, and the header is set only by the outermost cleanup-host middleware, which never runs on the active Host (B2). | A broker publish of the same type from any route is not delivered; no route can set the header. |
| D6 | Another tab of the old app, open or frozen, writes its data back after the first tab or the cleaning page emptied the origin. | Data restored under the old address after it was reported cleared. | Two layers, because only some stores have a counter. (a) Whoever clears leaves every write-fence counter (sign-out generation, local store epoch, boot-session generation) at the time of the clearing, a value no tab captured, instead of the reset that clearing causes. (b) It also leaves a marker. A tab that sees the marker (a storage event, or when it becomes visible, is shown or resumed) asks the instance; on a retired answer it clears itself and leaves through the old address's root, whose response clears once more. On an ordinary answer (the name was taken back and is this Avibe's address again) it removes the stale marker and clears nothing. | Page test: after clearing, all three counters hold a value above any captured one; a second document that receives the marker clears and leaves after a retired answer; a stale marker on a live address is removed and drafts stay. A pytest pins the key names in the cleaning page's script to the ones the app reads. |
| D5 | Pages open on this computer's local address. | Nothing: their data is under the local origin. | They get a plain `instance.address.changed` refetch signal (Owner only) and clear nothing. | Local page keeps its store across a rename. |

### E. The old address

| ID | Who could do what | What would be lost | Rule | Acceptance check |
|---|---|---|---|---|
| E1 | Any request on a cleanup Host: a page, an API call, a file, a share link, a sign-in callback, `/api/session`, `/auth/logout`, a static asset, `/health`. | Content, a session, or a sign-in on a name that is on its way to someone else. | One outermost middleware answers before routing, authentication, logging of request data, or reading the body. Every method and path gets the same `410` cleaning response, with three exceptions that are also fixed responses: Health device routes (E5), `/push-sw.js` (E3) and the receipt sink (F2). WebSocket upgrades are closed. No redirect, no path, query or cookie is forwarded anywhere. | Table test over the route inventory, including the paths exempt from host validation today (`/api/session`, `/auth/logout`, `/auth/callback`, `/api/csrf-token`, `/assets/*`, `/p/*`): identical response, handler never entered. |
| E2 | The browser that receives it. | Its stored data stays; or it is signed out of avibe.bot and its other instances. | Headers: `Clear-Site-Data: "cache", "storage"` (never `"cookies"`, never `"*"`: cookies would clear the whole registered domain); an ordinary expiring `Set-Cookie` for each fixed-name cookie of this origin (session, CSRF, the two sign-in handshake cookies, claim), matching its original attributes; and for **every cookie the request itself carries**, by its name, as a host-only cookie at each directory prefix of the request path (the request does not say which path a cookie was set on). This reaches the cookies whose name and path are per share: the Show guest lease at `/p/<share id>` (readable JSON with the guest's email, no expiry of its own) and the Show event token at `/show/<session>/`. Such a cookie is only ever sent on its own path, so every request that carries it also expires it. A `Set-Cookie` without a `Domain` cannot touch a cookie of the parent domain, so avibe.bot's own cookies survive. Names are checked against the cookie-name grammar and the number of headers is capped. Nothing is expired for shares the request did not name: that would list this instance's share ids to a stranger; `Cache-Control: no-store`; `Referrer-Policy: no-referrer`; a Content-Security-Policy that allows only the one inline script by hash and same-origin `connect-src`; `X-Avibe-Address: retired`. | Header test: exact directive list; no `cookies`. Browser test: a profile seeded on the old origin is empty after one visit, and a cookie set for a sibling `*.avibe.bot` test origin survives. A guest cookie for share X is expired by a request under `/p/X`, whatever its name, and no share id other than the request's own appears in any response. A cookie the browser holds for the parent domain, sent on the same request under the same name, survives. A request with a malformed or very long cookie name, or hundreds of cookies, gets a bounded, well-formed response. |
| E3 | A browser without the header (Safari before 17, embedded web views), or a sleeping service worker that wakes without any page. | Same data, not cleared by the header. | The body's inline script deletes every IndexedDB database it can list, both known names otherwise, localStorage (leaving the same write fence as D2), sessionStorage, Cache Storage, script-readable cookies, unsubscribes push and unregisters every service worker, then posts the receipt if the page was given one. `/push-sw.js` on a cleanup Host is a small replacement worker that, once activated, deletes the databases and caches, unsubscribes and unregisters itself. | Browser test with the header stripped: the script alone empties the profile. A registered old worker that updates from the cleanup Host unregisters itself. |
| E4 | A stranger opens the old address during the waiting period. | The new name; who lived here. | The page says one approved line, in the visitor's language, and one link to avibe.bot: "This Avibe has a new address. People who can access it can open it after signing in at avibe.bot." / 「这个 Avibe 换了新地址。能访问它的人，在 avibe.bot 登录后可以打开。」 (avibe.bot lists the instance at its current address for its Owner and for authorized members.) No new name, no owner, no instance id, no difference in the response for a visitor with or without a cookie except the receipt (F2). What a stranger learns: an Avibe was at this name and moved. The cookie a returning visitor still sends reaches only this computer, as before, and is never logged. | Response body and headers contain neither the active hostname nor any identifier. Log capture during cleanup requests holds no cookie, Authorization header or query string. |
| E5 | The iPhone App keeps sending Health data and its credential to the old address. | Health payloads delivered; the phone never stops. | Health device routes on a cleanup Host: `401` in the device error envelope (`device_unauthorized`), `no-store`, body not read, credential not looked up. The App stops and clears locally on `401`. | Upload with a valid old credential and a large body on a cleanup Host: `401`, body bytes not consumed, private store untouched. |
| E6 | The home-screen App, the iPhone App's WebView, a share link held by someone else. | As E1. | Same cleaning response. The home-screen App has its own storage and is cleaned when it is next opened; it then shows the moved line and must be added again from the new address (said in the preview). The iPhone App finds the new address in its instance list. | Covered by E1/E2 tests; hand check in the exercise for the home-screen App. |
| E7 | A flood of requests on the old address. | Load on this computer and on avibe.bot. | The cleaning response is constant work. The state read (B2b) is one at a time, on a thread of its own, at most once per 5 seconds counted from the end of a failed read. A bounded number of requests on a cleanup name wait for the read in flight without holding a thread; the rest get the `503` at once. Requests on the active name never wait for it. The receipt sink reads at most 256 bytes and writes only on a first match. | Forty concurrent requests on an old name while the read is slow: one outbound read, a bounded number wait and are then cleaned, the rest get `503` at once, and requests on the active name are answered meanwhile. A failed slow read is not repeated for 5 seconds after it ended. |

| E8 | A request on a managed name that is neither active nor a freshly listed cleanup name: a former name after its route was withdrawn, a quiet name, a name whose state is stale, someone else's name sent as Host. | Today the host check is skipped for some paths: on any Host `/api/session` answers as a local Owner, `/api/csrf-token` sets a cookie, `/auth/logout` runs, `/assets/*` and the Show runtime files are served. | The same outermost middleware gives one fixed `503` with no content, no cookie, no clearing header and no script on **every** path and method, and closes WebSocket upgrades. Refusing content by the name's shape is safe; only cleaning is never inferred from it. | For a managed name in each of the four states, every path of E1's inventory: identical `503`, handler never entered, no `Set-Cookie`, no `Clear-Site-Data`. |

### F. What the Owner sees

| ID | Who could do what | What would be lost | Rule | Acceptance check |
|---|---|---|---|---|
| F1 | The Owner reads the device list before and after. | Knowing which devices still hold data. | Before: the preview returns the devices that will be signed out (the registry's Owner view: every account's devices and push-only rows), with kind, name, account and last active. At the switch, in the same transaction and before the sessions are deleted, these rows are copied into a clearing list for the retired name. After: the state route returns that list with `state: "cleared" \| "unconfirmed"`, `cleared_at`, `last_active_at`. Owner and direct loopback only; members see nothing of it. | Snapshot equals the pre-rename Owner list. A member's request: `403`. |
| F2 | A device is marked cleared that was not; a member marks someone else's device. | A false "cleared". | "Cleared" is set only by a **receipt** posted to the old address after clearing. The receipt is a keyed one-way value: an HMAC of the session's random id under a random key that this instance keeps in its protected state directory (not the cookie-signing secret, not in the database); the list stores only a hash of each receipt. Knowing a session id is not knowing its receipt. A page gets its own receipt when its stream connects and in the final frame; the cleaning page gets it embedded only when the request's cookie carries a session id whose receipt is in the list. Nothing else marks a row: not a response having been sent, not a push. A receipt marks only its own session's device, once. A push-only row has no receipt and stays unconfirmed. | Receipt of session A cannot mark B. Random and replayed receipts change nothing. A request with a cookie but no script run leaves the row unconfirmed. A page whose clearing failed posts nothing. |
| F3 | The list reads as "everything is clean". | The Owner believes no browser holds data when some do. | The route always returns `complete: false` with the reason `sessions_expired_before_rename_not_listed` and the count of unconfirmed rows. It has no all-clear field. | Contract test: no state of the list yields `complete: true`. |
| F4 | A clearing list outlives its use. | Device and account metadata kept without purpose. | A name's list is deleted when the name leaves the cleanup set: taken back, waiting period over, or route withdrawn. | Take back a name: its list is gone. |

### Checked and unaffected

- **Protected Vault passkeys.** Their relying party is the sandbox origin
  (`vibe/ui_server.py` `_vault_sandbox_webauthn_origin`), not the instance's
  name, so a rename neither disables them nor hands them to a later holder of
  the old name.
- **Custom hostnames** of organization instances: rename is personal-only and
  the active-hostname snapshot is not a source of cleanup names.

## Route contract for the page

All three: Owner by current authorization or direct loopback (A1); JSON;
`Cache-Control: no-store`. Errors are `{ok:false, error:<code>}` plus fields
named below.

- `GET /api/instance-address` →
  `{ok, hostname, public_url, eligible, ineligible_reason, hold_days (from avibe.bot's `release_delay_seconds`; null until first read), release_at_if_changed_now (passed through; null when avibe.bot cannot give a date),
  operation, unrecognized_change, cleanup, health}`.
  `operation`: `null` or `{state:"pending"|"applying"|"failed", hostname, error}`.
  `unrecognized_change`: `null` or `{previous_hostname, hostname, noticed_at}`.
  `cleanup`: `[{hostname, retired_at, release_at (the real date from avibe.bot, passed through; it can be later than the waiting period when a direct certificate outlives it), complete:false,
  incomplete_reason, unconfirmed, devices:[{id, kind,
  display_name, account:{email}|null, last_active_at, push_only,
  state, cleared_at}]}]`.
  `health`: `{connected:boolean, cleanup:"none"|"pending"}`.
- `POST /api/instance-address/preview` `{prefix}` →
  `{ok, confirmation_id, expires_at, hostname, is_return, hold_days, release_at_if_changed_now,
  devices:[…as above without state], health_connected}`.
  Local refusals only: `invalid_prefix`, `same_address`, `rename_ineligible`,
  `operation_in_progress`. No availability check: the name is not asked of
  avibe.bot before the Owner confirmed.
- `POST /api/instance-address/change` `{confirmation_id}` →
  `200 {ok, state:"applied", hostname, public_url}` or
  `202 {ok, state:"pending"}` (the page polls the `GET`).

| HTTP | `error` | For the page to word |
|---|---|---|
| 400 | `invalid_prefix` | Naming rule. |
| 403 | `owner_required` | Not the Owner. |
| 409 | `confirmation_expired` | Preview again. |
| 409 | `operation_in_progress` | A change is already running. |
| 409 | `rename_ineligible` | Organization instance, not paired, or refused by avibe.bot. |
| 409 | `taken` | The name is not available. No reason is given, by design. |
| 409 | `name_holding_limit_reached` + `limit`, `held_count`, `earliest_release_at` (UTC string or null) | Approved sentence ("already holds N names, including those still waiting") with the real number and date, passed through from avibe.bot unchanged. The page does not hold the number. |
| 409 | `address_changed` | The address changed meanwhile; reload. |
| 409 | `pairing_invalid` | avibe.bot no longer accepts this binding: bind remote access again. |
| 503 | `unavailable` | avibe.bot could not be reached or could not decide; nothing changed, or the operation stays pending. |

The old address (not for the settings page): `410` cleaning response on every
path; `POST /__avibe/address-cleared` with the receipt as the body, answered
`204` with the same headers except `Clear-Site-Data: "cache"`; `/push-sw.js` the replacement worker; Health
device routes `401`.

## Contradictions found

1. **Brief ("every session ends", contract item 4 "rotate session epoch") vs
   the device registry.** The registry's binding is derived from the instance id
   and the cookie-signing secret. Rotating that secret, as turning remote access
   off does, would orphan every registry row, the 90-day activity history and
   the Health links, and the after-rename device list could not be tied to the
   pre-rename one. Rule C1 therefore ends sessions with the registry's own
   all-remote sign-out and keeps the secret.
2. **Contract item 3 (authoritative role "per request", "first version may do
   an online state check per request") vs the product boundary.** The instance
   cannot ask avibe.bot before serving each request: avibe.bot is not on the
   data path, and the instance must keep working when it is unreachable. B2 and
   B3 are the cached design the contract allows: content needs no online check;
   cleaning needs a state at most 60 seconds old and fails closed; the instance
   quiets a name before asking for it back, which is the drain barrier for the
   only reactivation it can initiate.
3. **Contract item 6 (`"storage"` only) vs the code.** Some authenticated
   responses are cached `private, immutable` for up to a year
   (`vibe/ui_server.py` bot avatar, local Show icon). `"cache"` is added.
4. **Sign-out's clearing vs the brief's list.** Sign-out today leaves drafts,
   recent files, window layout, the notification device id, Cache Storage, the
   service worker and the browser's push subscription. Rename clearing is a
   separate, complete function (D2); sign-out is not changed here.
5. **Brief item 5 (list from the registry) vs pruning.** The registry deletes
   expired sessions and never knew sessions older than itself; F3 is the
   consequence.
6. **The waiting period as "one number" (30 days since 11:19).** The sign-in
   cookie lives up to 30 days in the browser after its last renewal, and the
   last renewal is no later than the switch. So 30 days is the floor with no
   margin: a smaller number, or a browser whose clock runs behind, lets a later
   holder receive a visitor's email in a cookie from a browser that never
   returned. For deleted instances there is no cleaning response to expire the
   cookie earlier. Handled on avibe.bot: 48 more hours pass before a name is
   actually given out, and no earlier than our last direct certificate for it
   expires.

## Contract points to settle

Settled with the backend writer (Session `ses6x9a627t9a`, November 21, 11:27):

1. Content only on the locally recorded active name, never an online check.
   Cleaning: at most 60 seconds from the start of the state read, one read at a
   time, at most one per 5 seconds, quiet-before-return and local drain (B2,
   B3), plus the backend's own barrier for a return from any requester.
2. Successful `runtime-status` and authorization-revision replies carry an
   integer `hostname_generation`. It is a hint to read `hostname-state`, never
   a permission to clean.
3. `hostname-state` carries `release_delay_seconds` (30 days now). The page
   takes the number of days from it. avibe.bot keeps 48 more hours before the
   name is actually given out; users are told 30 days.
4. A commit changes `public_url`, `redirect_uri` (`public_url` +
   `/auth/callback`) and the returned `oidc_client_id`. Issuer, the three
   endpoints, the tunnel token and the device secret do not change; the
   cookie-signing secret is not rotated (C1). The connector is not restarted:
   its recorded signature includes `public_url`, so the switch updates that
   record (B1), or the next Start or reconcile would bounce the tunnel.
5. Every path on a cleanup name answers `410`, `/health` included; avibe.bot
   probes no cleanup name. Health device routes are the exception: `401
   device_unauthorized`, body unread (the App acts on `401`, not on `410`).
6. `Clear-Site-Data: "storage", "cache"`; never `"cookies"` or `"*"`.
7. On `401` from `hostname-state` every cleaning permission is dropped and no
   late reply restores it; the active name keeps today's behaviour.

The limit on names comes back as `409 name_holding_limit_reached` with
`limit`, `held_count` and `earliest_release_at`.

A `429` or `5xx` from avibe.bot is not shown to the page: the same request is
repeated and the operation stays `pending`.

Settled with the direct lane (coordinator Session `ses8mgme5623t`): C3.

Dates (settled 11:33): a waiting name has one date, `release_at`: 30 days
plus 48 hours from the rename, or the expiry of our last direct certificate for
it, whichever is later. Until then only the former holder can take it back and
the name stays routed here for cleaning; at that date the route is withdrawn
and the name returns to the pool. The instance cleans a name for as long as a
fresh state lists it and no longer than the date avibe.bot gave; it never
computes the date. Every date shown is `release_at`; text without a date says
"30 days". The state also carries `active_release_at_if_renamed_now`, an
estimate ("if changed now, no earlier than") that may be null: the page then
shows no date. After a commit the receipt's `previous_release_at` and the
latest state are what is shown. None of these fields decides whether a name
has actually returned to the pool.

## Residuals, in plain words

- **A browser that never returns.** A browser that had no page open at the
  rename and does not open the old address during the waiting period keeps its
  data. Chrome, Firefox and installed apps never delete it by age. When it
  later lands on the old address, the holder at that time can read it. This
  includes the Owner's rarely used devices, a member's work computer, a
  home-screen App that was not opened, and browsers whose sign-in expired long
  before the rename and so appear in no list.
- **The old address cleans only while this computer is on, connected and
  bound.** A laptop that sleeps most of the day, remote access turned off, or
  avibe.bot unreachable (no fresh state, so no cleaning) means a returning
  browser meets an error page, not a cleaning one, and keeps its data.
- **Share guests.** A person who opened a shared page as a guest holds a
  cookie with their email, scoped to that share's path, that never expires by
  age in a browser that restores sessions. It is expired when that browser
  opens the old link during the waiting period. Guests are in no device list.
  A guest who returns only later sends that cookie to whoever holds the name.
- **Links already sent.** Links with the old name that this Avibe put into
  chats and mail are dead during the waiting period; afterwards they lead to
  whoever holds the name.
- **A response already on the network.** A cleaning response sent just before
  a name becomes active again can reach a slow browser afterwards; that
  browser loses its cache and signs in again.
- **The device list under-reports.** "Unconfirmed" covers both "still holds
  data" and "will never be opened again". The list cannot say everything is
  clean and does not try.
- **Browsers without the header and without script**, and web views inside
  other apps, are not cleared by a visit.
- **Safari.** Measured with WebKit in the browser suite: storage is emptied
  and the page's script runs to its last step (the report arrives), but the
  page is ended right after. With the header, a background script the page
  controlled can stay listed until that script next checks for an update, when
  the replacement removes it; and the write fence the script leaves (below) is
  not always seen by the next page.
- **What stays under the old address after cleaning.** Three counters and a
  marker in localStorage, all set to the time of the clearing. They stop a tab
  of the old app that is still open elsewhere from writing its data back, and
  make it clear itself (D6); clearing alone would reset them. They name nobody.
- **A second tab that is frozen.** All tabs of one browser share one sign-in,
  so the first tab's receipt marks the device cleared while a frozen tab still
  holds its page in memory. When that tab comes back it sees the marker, asks
  the instance, clears and leaves through the old address; a tab that is never
  brought back writes nothing.
- **An iPhone App not opened during the whole waiting period** may open its
  saved old address once before it refreshes its instance list; the App is not
  changed in this work.
- **Direct path after a rename.** It is unavailable for a few minutes while
  the new certificate is issued; the tunnel works meanwhile. Changing back and
  forth more than twice in 7 days hits the certificate rotation limit, and the
  direct path stays off until the window passes. If remote access was bound
  again between a rename and taking the name back, an unfinished certificate
  order of the old binding can keep the direct path off for that name until it
  is cleared by hand. With the direct path off at the time of the rename, the
  old name's key and certificate files stay on this computer until the direct
  path is next turned on or remote access is bound again.
- **A certificate obtained by the tenant from a public authority** while
  holding the name can live about 200 days, longer than the waiting period.
  Our own direct certificates live 90 days: avibe.bot gives a name out no
  earlier than the last such certificate's expiry, so the real release date
  can be later than the waiting period, and every date shown is that real one.
- **Deleted and transferred instances.** After an instance is deleted, or a
  live instance is transferred, its old names answer nothing: returning
  browsers are not cleaned.
- **A stranger learns** that an Avibe was at the old name and moved.
- **A rename made with a copied device secret** costs every visitor their
  local cache and unsent drafts and signs everyone out; data on the computer,
  Health included, is kept, and the Owner recovers the name after binding
  again.
- **Permissions granted to the old origin** (notifications, microphone)
  cannot be withdrawn by a page or a response and stay with the name.

## Verification

- `tests/test_instance_address.py`: the rules above against the real config
  file and SQLite state, with a fake of the agreed wire contract.
- `ui/src/lib/addressRetired.test.ts`: the page's own clearing (D2, D3).
- `ui/e2e/address-retired` (`npm run test:address-retired`, in the browser
  lanes of CI): Chromium and WebKit, under the repository's browser isolation,
  given the instance's exact answers for a retired address: one visit empties a
  seeded origin and reports; the script alone does; the header alone does and
  leaves cookies alone; a background script that updates removes itself.
- Then, once the backend half is live, the reviewed head in a fresh temporary
  Runtime in the local Incus regression environment, bound under the test
  account on Production: rename, a browser that held data on the old address
  shown empty on its next visit, take the name back, a foreign change shown
  non-destructive. The exact steps are a separate file for the lead.

## Todo

- [x] Agree this table (reviewer) and the contract points (backend, direct).
- [x] Migration 0097 for the clearing list.
- [x] Journal, role table, state reader, routes (`vibe/instance_address.py`).
- [x] Cleanup-host middleware, cleaning page, replacement worker
      (`vibe/ui_server.py`, `vibe/instance_address_responses.py`).
- [x] Page: complete clearing, final frame, header handling, receipt
      (`ui/src/lib/addressRetired.ts`).
- [x] Focused tests and the browser suite.
- [ ] Exercise on Production after the backend half is live.
