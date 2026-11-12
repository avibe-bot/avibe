# Remote direct paths: the App's iroh transport (A2)

Status: **Core built and inactive; not linked, not registered, not qualified.** This is row A2 of [`remote-direct-phase1.md`](remote-direct-phase1.md) and the transport side of [`remote-direct-ios.md`](remote-direct-ios.md) section 12. The code in this document's pull request compiles and is tested against stand-ins for the iroh binding. No App build links an iroh binary (`packaging/iroh-apple/release.json` is `pending`), nothing in the App creates the transport, and `AvibeApp` registers none. Section 8 lists what is left for the first build that uses a transport; that build is the owner's to approve.

Statements below are **rule** (what the code does), **read** (taken from the pinned source, not run), or **proposal** (a bound to be set from measurement).

## 1. Pieces

| File | Holds |
| --- | --- |
| `DirectRendezvous.swift` | The answer model, its validation against the selected instance, and the address check. Pure. |
| `DirectRendezvousFetcher.swift` | The native same-origin request (decision D3): a fixed script in `WKContentWorld.defaultClient` of the runtime's WebView. |
| `DirectIrohEngine.swift` | `DirectIrohEngine` and its companions: the subset of the generated binding the transport uses, as protocols. Also `directIrohCall` and `directIrohDeadline`, the timing proposals, the timer seam, and the `NWPathMonitor` wrapper. |
| `DirectIrohByteStream.swift` | One stream over the binding: `read`, `write`, `closeWrite`, `cancel`. |
| `DirectIrohTransport.swift` | The transport: `select`, `deselect`, `pathDidFail`, `openStream`, the probe, and the reports. |

The persistent-proxy seam (`DirectTransporting`, `DirectByteStream`, `DirectTarget`, `DirectStreamTag`, `DirectPathReport`) is in #3537, which is not merged. This code therefore names its own small value types (`DirectIrohSelection`, `DirectIrohStreamTag`, `DirectIrohReport`) with the same content, and its methods have the seam's shapes. Section 8 has the two conformances that join them.

## 2. What the generated binding does (read)

From `IrohLib.swift` of the gate artifact and the iroh-ffi source at the pinned commit (`packaging/iroh-apple/versions.env`: v1.2.0, `66a8d619`). These shape the design, and each is a difference from what `remote-direct-ios.md` section 12 assumed or left to verify.

1. **No call can be cancelled.** `uniffiRustCallAsync` polls the Rust future through `withUnsafeContinuation` and passes no cancellation on. A cancelled Swift task keeps waiting until the Rust future completes. This holds for `Endpoint.bind`, `connect`, `Connection.openBi`, `closed`, and every stream call.
2. **A stream half is a lock held across its own awaits.** `RecvStream.read` holds the half's mutex while it waits for bytes, and `stop` takes the same mutex; likewise `SendStream.writeAll` and `reset`. So `stop` cannot interrupt a read in progress, and `reset` cannot interrupt a write blocked on flow control.
3. **The only call that ends everything at once is closing.** `Connection.close` is synchronous and fails every stream call on that connection. `Endpoint.close` also ends a dial in progress. A pending `connect` has no other abort: `connectPending` hands back a handle, but its `connect()` takes the attempt out of the handle, so dropping the handle ends nothing.
4. **End of stream is an empty buffer.** `RecvStream.read(sizeLimit:)` returns empty `Data` when the peer finished, and never otherwise for a non-zero limit. It allocates `sizeLimit` bytes for each call.
5. **`finish` does not wait** for delivery, and a later `reset` drops what is not delivered yet. Dropping a send half finishes it implicitly.
6. **No keep-alive or idle-timeout setting** in `EndpointOptions`, and no dial timeout. The instance's sidecar carries the 6 s idle timeout and 1.5 s keep-alives, which QUIC applies to both peers.
7. **`EndpointOptions.preset` defaults to the n0 preset** (relays and discovery). The transport's engine must always pass `presetMinimal()` and `RelayMode.disabled()`, and must never construct `ServicesClient`.
8. **`EndpointId.fromString` reads base32.** The contract's 64 hex characters go through `fromBytes`.
9. **The wrapper's type names are general** (`Endpoint`, `Connection`, `Side`, `Preset`, `Signature`, `LogLevel`). If A1 compiles `IrohLib.swift` into the App target they enter the App's namespace. A separate module for the wrapper would keep them out; that is A1's choice, and only the engine file depends on it.

## 3. Staying prompt on a binding that cannot be cancelled

- **`directIrohCall`** runs one binding call so that its caller leaves as soon as the caller's task is cancelled. The binding's call is left to finish, and a value it still produces goes to a `discard` closure: a late endpoint or connection is closed, a late stream is abandoned. Nothing is started for a caller that is already cancelled.
- **`directIrohDeadline`** is an ordinary structured deadline around operations that are prompt in that sense.
- **Closing is the bound on what was left running** (rule). Every path that ends a session closes its connection and then its endpoint, which ends every binding call still pending on them. An abandoned dial ends when its endpoint closes. So no binding call outlives the session it belongs to.
- **`DirectIrohByteStream.cancel()`** ends the stream's calls in progress at once and drops their late bytes. It then asks the binding to reset the sending half and stop the receiving half. Because of item 2 above, each request waits for that half's call in progress:
  - In the common case one half is free. The sidecar ends both directions when either is reset or stopped (`direct-transport/src/main.rs`, `forward_stream`), so the other half's pending call ends one round trip later.
  - On a dead network, the pending calls end when the connection closes: through the idle timeout, or at once when the transport ends the session.
  - If both halves are busy (a read waiting and a write blocked on flow control) on a live connection, the binding's calls stay until the instance makes progress. The App's side has already returned; one of the peer's 64 stream slots stays taken until then. Nothing in the stock binding can shorten this.
- **An abandoned call poisons its stream** (rule). When a caller's task is cancelled during a read or a write, bytes are unaccounted for, so the stream cancels itself and is never used again.
- **`cancel()` does not undo a clean end** (rule). After `closeWrite` returned, the sending half is not reset, so bytes already written are still delivered; after the instance's end of stream, the receiving half is not stopped. A stream released without `cancel()` is abandoned the same way, never finished as if complete.

## 4. Before any socket

Nothing exists until a rendezvous answer is `ready` and accepted (rule): no endpoint, no UDP socket, and so no Local Network prompt.

- **The launch key** is made at the first selection, kept in memory, and never stored. Making it opens nothing. Its public half is the `client_endpoint_id` of every request in this launch.
- **The request** is a `POST` to an absolute URL native code builds from the selected instance's origin. The script refuses to run unless its document's origin is that origin, and when a service worker controls the document. It copies the CSRF header from the `vibe_csrf_token` cookie, refuses redirects, checks the response's final URL, reads at most 16 KiB, and ends at its deadline (5 s, proposal). The answer returns through `callAsyncJavaScript`. No bridge method accepts an answer, and page script cannot reach the content world the script runs in.
- **An answer is accepted only if** its `version` is 1; `instance_id` and `hostname` are the selection's; `alpn` is `avibe-direct/1`; `client.endpoint_id` is this launch's; `endpoint.id` is 64 lowercase hex and not this launch's own; `generation` is 1 to 128 bytes; and the answer's own span is positive.
- **Lifetime** is the answer's own span, `min(expires_at - issued_at, client.admitted_until - issued_at, 300)` seconds, counted from receipt. The phone's clock is not compared with the instance's. This reads the contract's "`expires_at` is in the future" as "the answer has not run out since native code fetched it": a wrong clock on the phone must neither refuse a fresh answer nor keep a stale one.
- **Addresses.** Only numeric socket addresses of the class their `kind` names: `lan` is 10/8, 172.16/12, 192.168/16, or fc00::/7; `ipv6` is 2000::/3 without 2001::/23, 2001:db8::/32, 3fff::/20, and 2002::/16. Everything else is dropped, including IPv4-mapped and NAT64 forms, zones, names, and unknown kinds. The first 32 entries are looked at and at most eight kept. What is dialled is written again from the parsed bytes. An answer with no admitted address is treated as a failure.
- **`unavailable`** asks again after `retry_after_seconds`, kept between 30 s and 15 min.

## 5. One path at a time

A **session** is one endpoint and one connection for one accepted answer.

1. **Dial.** Bind an endpoint for the launch key, then dial the answer's endpoint ID at the admitted addresses with the ALPN, in one `connect` (bounds: 2 s and 1.5 s, proposals). The peer's identity is compared again after the handshake.
2. **Probe.** Open a stream, write the canonical CONNECT head, and read the instance's `200` (1 s, proposal). The probe's stream is then abandoned. Only now is `usable(generation)` reported.
3. **Refresh.** The answer is asked again at 80 % of its lifetime, so the admission is extended before the sidecar ends it. The same generation and endpoint keep the session. A failed refresh keeps it too and is tried again after 15 s.

| Event | Session | Report | Next |
| --- | --- | --- | --- |
| The connection closes (idle timeout, admission ended, sidecar gone) | Ended | `lost` | Ask again, by the retry ladder |
| `pathDidFail` for the current tags | Probed; kept if the probe passes | `lost` only if the probe fails | Ladder after a failure; at most one such probe a second |
| A network change | Kept, not usable for new streams | `verifying`, at once and once | Probe after 0.5 s of quiet |
| That probe passes | Usable again; its streams were never touched | `usable`, same generation | Ask the rendezvous again |
| That probe fails | Ended | `lost` | Ask again at once |
| A refresh answers with another generation, or `unavailable` | Ended before anything new is dialled | `lost`, then `usable(new)` when the new path answers | Dial the new answer, or wait as told |
| A network change with no session | — | — | Whatever was under way stops; start over after 0.5 s |
| `deselect`, a new `select` | Ended | none | — |
| The App enters the background | Ended | `lost` if a session was up | Start over on the return, if still selected |

- **Retry ladder** (proposal): 0.25 s, 1 s, 5 s, 15 s, 60 s, then 5 min. The first step is short because an admission notice can lose the race with a fast dial. The count is cleared by a path that stays usable for 30 s, and by a network change or a return to the foreground, so a path that flaps backs off.
- **A page that is still loading** (`pageNotReady`) is asked again after 0.5 s, up to 40 times, before it counts as a failure.
- **`lost` is reported after the next attempt is arranged,** so the controller already sends new connections to the public origin when the report arrives.
- **The background** is handled by the transport itself as well as by the controller's `deselect`: no endpoint or socket outlives `didEnterBackground`.

## 6. Fencing

Every piece of work (a request, a dial, a probe, a session) carries a stamp, and a result whose stamp is no longer current changes nothing; whatever it produced is closed (rule).

`openStream(for:)` returns a stream only if, both before and after the binding's open:

- the tag's selection is the current one;
- the tag's rendezvous generation is the live session's;
- that session is usable, not being verified.

A stream that opens after any of these stopped holding is abandoned and the call throws. Ending a session cancels every stream opened on it.

**Path generations.** The controller issues them and the transport cannot know the current one. It keeps the highest generation it has seen in a tag, and after each report that ends or suspends a path it refuses tags at or below that mark: the controller advances its generation on each such report, so those tags are stale. This catches only tags the transport has seen a successor of. The controller's own check at the commit point stays the authority.

## 7. Tests, and what they do not show

In `ios-shell`, on the simulator:

| Invariant | Tests |
| --- | --- |
| Address classes, spellings, and every refused form | `DirectRendezvousTests.testOnlyPrivateIPv4UniqueLocalAndGlobalIPv6AddressesAreAdmitted` |
| An answer is accepted only for the selected instance and this launch; every rejection | `testAReadyAnswerForTheSelectedInstanceIsAccepted`, `testAnAnswerThatDoesNotMatchTheSelectionIsRejected` |
| Lifetime; at most eight addresses; unknown entries skipped; `unavailable` | `testTheLifetimeIsTheAnswersOwnSpanAndAtMostFiveMinutes`, `testAtMostEightCheckedAddressesAreKeptAndTheRestOfTheListIsSkipped`, `testAnUnavailableAnswerSaysWhenToAskAgain` |
| The request is the instance's own (method, CSRF, cookie, Origin, body); refusals, redirects, size, and deadline; only on the instance's origin | `DirectRendezvousFetcherTests.testTheRequestIsTheInstancesOwnAndItsAnswerComesBackUnchanged`, `testNothingIsAskedBeforeTheInstancesDocumentAndItsCookieAreThere` |
| A page cannot answer in the instance's place or move the request | `testAPageCannotAnswerInTheInstancesPlaceOrMoveTheRequest` |
| A cancelled caller leaves at once; late values are discarded; nothing starts after cancellation; deadlines | `DirectIrohByteStreamTests`, first three tests |
| Bytes, end of stream, backpressure, half-close | `testBytesPassBothWaysAndTheEndIsNil`, `testAWriteReturnsOnlyOnceTheTransportAcceptedIt` |
| `cancel()` and task cancellation end calls in progress at once; the stream is over; only open halves are abandoned | `testCancelEndsAReadAndAWriteInProgressAtOnce`, `testACancelledCallerEndsItsReadAtOnceAndTheStreamWithIt`, `testCancelAbandonsOnlyTheHalvesThatHaveNotEnded`, `testAStreamReleasedWithoutCancelIsAbandonedNotFinished` |
| Nothing is bound before an accepted `ready` answer | `DirectIrohTransportTests.testNothingExistsBeforeAnAcceptedReadyAnswer`, `testAHostThatIsNotADNSNameIsNeverAskedFor` |
| Dial, identity, probe, and only then `usable` | `testAReadyAnswerIsDialledProbedAndOnlyThenReportedUsable`, `testAnotherIdentityOrARefusedProbeIsClosedAndNeverUsable` |
| Bounds, `deselect`, and late results | `testADialThatDoesNotAnswerEndsAtItsBoundAndALateConnectionIsClosed`, `testDeselectEndsADialAtOnceAndWhatArrivesLateIsClosed`, `testAnAnswerThatArrivesAfterDeselectChangesNothing` |
| Each row of the table in section 5 | `testAClosedConnectionIsLostItsStreamsEndAndTheRendezvousIsAskedAgain`, `testANetworkChangeIsVerifyingThenUsableOnTheSamePathOrLost`, `testANetworkChangeBeforeAnyPathStartsOver`, `testAFailedCandidateProbesThePathAndOnlyADeadPathIsLost`, `testARefreshKeepsTheSameGenerationAndRetiresAReplacedOne`, `testFailuresBackOffAndAPageThatIsStillLoadingIsAskedAgainSoon` |
| Stream fencing and late opens | `testAStreamOpensOnlyUnderCurrentTags`, `testAStreamThatOpensAfterItsPathEndedIsAbandonedNotReturned`, `testACancelledOpenEndsAtOnceAndItsLateStreamIsAbandoned` |
| Nothing lives in the background | `testNothingLivesInTheBackground`, `testATransportCreatedInTheBackgroundDoesNothingUntilTheForeground` |

**Not shown by any of this:**

- Anything about the real binding. The stand-ins copy the properties in section 2 from reading the source; no test here runs iroh. The engine over the generated types is compiled and tested for the first time with A1.
- Interoperability with the instance's sidecar, and the dead-path timings, which only the real sidecar's idle timeout produces.
- Devices: the Local Network prompt and its denial, real Wi-Fi, IPv6 and cellular, suspension, battery, and iOS 17.
- Every bound in `DirectIrohTiming`. They are proposals.

## 8. Left for the first build that uses a transport

In order, each after its dependency lands. None of it is in this pull request.

1. **After A1 links the gated binary:** `DirectIrohBinding`, the `DirectIrohEngine` over the generated types (about 150 lines, one type per protocol in section 1). With it:
   - tests with two real endpoints in one process over loopback: bind (no relay, the ID from the key equals `endpoint.id()`), a wrong identity, the stream mapping, and cancellation of each call against the real binding;
   - the interoperability test against the published sidecar.
2. **After #3537 merges:**
   - `extension DirectIrohByteStream: DirectByteStream {}` and `extension DirectIrohTransport: DirectTransporting`, which maps `DirectTarget`, `DirectStreamTag`, and `DirectPathReport` to this file's types;
   - the rendezvous closure: the runtime's primary WebView for the target, only while that runtime is current and its store is the target's, passed to `DirectRendezvousFetcher`;
   - the transport passed to `AppModel` by `AvibeApp`. This is the line that makes the App use a transport, and it is the owner's to approve.
3. **With that build:** `NSLocalNetworkUsageDescription` in both locales, and the interplay with #3537's tests (`DirectPathAppTests`' shipped-App test must keep passing until the transport is registered, and then be replaced by its positive control).

## 9. Open

1. **D3 stays the iOS owner's.** This implements the first option (a fixed script in the runtime's WebView). It can run only while that WebView shows a document of the instance. A second hidden WebView on the instance origin would not depend on the page, at the cost of one more WebView per account.
2. **D4.** No preflight detects a denied Local Network permission: a denied dial fails like an unreachable address and the App stays on the public origin. Recognizing denial earlier would only save dials; it needs a device to design.
3. **Both halves busy** (section 3). If device measurement shows cancelled streams lingering in practice, the fix belongs upstream (a cancellable read) or in a patched binding, which the owner has not approved.
4. **The sidecar closes a connection that has had no stream for five minutes.** An App left open on an idle page then sees `lost` and dials again. Whether to keep one idle stream instead is a measurement question.
