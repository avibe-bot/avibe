# Remote direct paths: the App's iroh transport (A2)

Status: **Core written and inactive; not linked, not registered, not qualified.** This is row A2 of [`remote-direct-phase1.md`](remote-direct-phase1.md) and the transport side of [`remote-direct-ios.md`](remote-direct-ios.md) section 12. The pull request that adds this document holds the core and its tests against stand-ins for the iroh binding; the `ios-shell` result for its head is in that pull request, not here. No App build links an iroh binary (`packaging/iroh-apple/release.json` is `pending`), nothing in the App creates the transport, and `AvibeApp` registers none.

This is not the whole of A2. The binding adapter, the join to the proxy seam, the registration, and everything that runs real iroh are still to be implemented and qualified (section 8). A build that registers a transport is a separate gate with its own approval and evidence; nothing here grants it.

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
- **`directIrohDeadline`** is a structured deadline around operations that are prompt in that sense. The deadline and the operation can finish in the same instant; a value that loses to the deadline goes to the same `discard`, so it is returned or closed, never dropped.
- **Closing is the bound on what was left running** (rule). Every path that ends a session closes its connection and then its endpoint, which ends every binding call still pending on them. An abandoned dial ends when its endpoint closes. So no binding call outlives the session it belongs to.
- **`DirectIrohByteStream` owns each half's state, and every change of it happens under one lock** (rule). The sending half is open, writing, finishing, finished, or abandoned; the receiving half is open, reading, ended, or abandoned.
  - **A binding call that is started runs to its end and records its own outcome.** Starting it and recording that it runs are one step, so a half is never left in a state that no call will settle. The call's completion and `cancel()` take the same lock: one comes first and the other sees it.
  - **`cancel()` first:** the caller gets `cancelled` at once and never the call's result. A read does not report bytes, or a clean end, from a stream that was cancelled before the binding answered.
  - **A clean end first:** it stays. After the instance's end of stream the receiving half is `ended`: later reads answer from that record without asking the binding, and nothing stops the half. When `closeWrite` returns, the sending half is `finished` and is never reset.
  - **`closeWrite` hands the half over.** From the step that starts `finish`, `cancel()` does not reset the sending half: the bytes written before are still to be delivered. The call that started `finish` owns the half until it records `finished`, or, if `finish` fails, resets it. Nothing in the binding's contract says a failed `finish` closed the half, so a failure is never taken for a clean end.
  - **Abandoned exactly once.** A half that did not end cleanly is reset (sending) or stopped (receiving) once, by whichever of `cancel()`, a failed call, or the stream's release moved it to `abandoned`.
  - **Refused without reaching the binding:** a second read or write while one is in progress, `closeWrite` during a write or a second time while the first is in progress (`busy`); a write after `closeWrite` (`closed`). `closeWrite` after it returned does nothing.
  - **A caller that is already cancelled starts nothing,** and the stream is cancelled.
- **What `reset` and `stop` can do in the binding.** Because of item 2 above, each waits for that half's call in progress:
  - In the common case one half is free. The sidecar ends both directions when either is reset or stopped (`direct-transport/src/main.rs`, `forward_stream`), so the other half's pending call ends one round trip later.
  - On a dead network, the pending calls end when the connection closes: through the idle timeout, or at once when the transport ends the session.
  - If both halves are busy (a read waiting and a write blocked on flow control) on a live connection, the binding's calls stay until the instance makes progress. The App's side has already returned; one of the peer's 64 stream slots stays taken until then. Nothing in the stock binding can shorten this short of closing the connection, which would also close every healthy relay on it.
- **An abandoned call poisons its stream** (rule). When a caller's task is cancelled during a read or a write, bytes are unaccounted for, so the stream cancels itself and is never used again.
- **A prompt return in Swift is not a cancellation in the binding.** After any abandoned call, a Rust future, its Swift task, and for a stream a slot at the peer, stay until one of the events above. The stand-ins show only the App's side. Whether those leftovers end, and how many can pile up under repeated cancellation, is shown only against the real binding (section 8) and is a condition of qualifying the transport, not something this document settles.
- **An abandoned call poisons its stream** (rule). When a caller's task is cancelled during a read or a write, bytes are unaccounted for, so the stream cancels itself and is never used again.
- **A stream released without `cancel()`** has no call in progress, or it would still be held. Its halves that are still open are abandoned, never finished as if their bytes were complete.

## 4. Before any socket

Nothing exists until a rendezvous answer is `ready` and accepted (rule): no endpoint, no UDP socket, and so no Local Network prompt.

- **The launch key** is made at the first selection, kept in memory, and never stored. Making it opens nothing. Its public half is the `client_endpoint_id` of every request in this launch.
- **The request** is a `POST` to an absolute URL native code builds from the selected instance's origin. The script refuses to run unless its document's origin is that origin. It copies the CSRF header from the `vibe_csrf_token` cookie, refuses redirects, checks the response's final URL, reads at most 16 KiB, and ends at its deadline (5 s, proposal). The answer returns through `callAsyncJavaScript`. No bridge method accepts an answer, and page script cannot reach the content world the script runs in.
- **A service worker is refused for the whole request** (rule). A content world does not keep a worker out, and neither does `cache: "no-store"`: a worker that controls the document answers its requests. The script listens for a change of controller before it first looks, and looks again before the request, after the response arrives, after each read of the body, and before it answers. Any controller at any of these moments, or any change in between, refuses the answer (`controlled`), which the transport treats as a failed request.
  - **Why looking afterwards is enough** (read, from the Service Workers specification): a request is intercepted only by the document's controller, and a document that has a controller keeps one until it unloads. So an interception leaves a controller to find.
  - **What is and is not shown.** The refusal at each moment is tested against a stand-in container placed in the script's own content world. One of its cases lets go of control again, which a real worker does not do; it only shows that a change is remembered. It has never met a real worker, because this App's WebViews have none: `testThisAppsWebViewsHaveNoServiceWorkerContainer` holds that `navigator.serviceWorker` is undefined in both worlds, in a secure context. If that test ever fails, a real worker can exist, and the refusal must be qualified against one before any build uses the direct path. On devices and on iOS 17 this is still to be checked (section 7).
- **Either answer speaks for one instance** (rule). `instance_id` and `hostname` are checked on `unavailable` as on `ready`. An answer for another instance, or for none, is a failed request: it brings no path up and takes none down.
- **An answer is accepted only if** its `version` is 1; `instance_id` and `hostname` are the selection's; `alpn` is `avibe-direct/1`; `client.endpoint_id` is this launch's; `endpoint.id` is 64 lowercase hex and not this launch's own; `generation` is 1 to 128 bytes; and the answer's own span is positive.
- **The lease.** An accepted answer becomes a lease: the answer and a deadline, `min(expires_at - issued_at, client.admitted_until - issued_at, 300)` seconds after **the moment the request was sent**, on a clock that never runs backwards (rule; rendezvous contract, section 5). The instance issued the answer after that moment, so the App's deadline is never later than the instance's own, whatever the phone's wall clock says and however long the answer took.
- **The deadline is data; its timer is only a wake-up** (rule). It is enforced in two places, because a decision on the main actor and the network action it leads to are separated by task hops and awaits:
  - **Where a network action starts.** Each owner of network work carries a `DirectIrohAuthority`: the deadline, a revocation, and for a path its suspension count. It is asked in the very step that calls the binding, on whatever thread that is, with no await in between: before the bind, before the connect, before a probe's open, its CONNECT, and each of its reads, and before a stream's open. An action it refuses does not start, and the step that finds it refused closes what the stage before produced. A bind or a connect that ends after the deadline is therefore followed by nothing, and a probe or an open that was set going before the deadline but reaches the binding after it starts nothing.
  - **Where the main actor commits or reports.** `reconcileLease()` compares the clock with the deadline before a stream is admitted or adopted, before a probe is started, when a request, a dial, or a probe ends, on `pathDidFail`, on a network change, and when the lease's wake-up or the next-request timer fires. The stability and probe-spacing timers only keep counts and start nothing.
  
  So nothing is started, committed, or reported on a lease the clock has passed, however late its timer runs, and a timer that fires early only sets itself again. What already started may still end late; it authorizes nothing after it. An answer of the same generation that arrives after the lease ran out renews the path only through a probe, whether or not the timer had fired.
- **Which authority can be renewed.** A dial's and a probe's are fixed at the deadline of the lease they started under and are revoked with them, so an old dial never borrows a newer answer's time: an answer accepted during a dial replaces the dial. Only a path's authority, for its streams, is renewed by a newly accepted answer of the same generation; and it refuses whatever was admitted before the path was last suspended. Streams that are already committed are not asked: they live until their connection ends.
- **The rendezvous request** has no lease. It is sent only if it is still the current request in the step that sends it. An answer that arrives with less than 30 s of its span left (proposal) is not accepted, so an instance that sends short-lived answers is asked no faster than the retry ladder.
- **Only a newly accepted answer replaces the lease** (rule). A refresh that fails, arrives late, or belongs to another selection extends nothing. When the deadline passes, nothing is opened, dialled, or probed under the answer any more (section 5).
- **Addresses.** Only numeric socket addresses of the class their `kind` names: `lan` is 10/8, 172.16/12, 192.168/16, or fc00::/7; `ipv6` is 2000::/3 without 2001::/23, 2001:db8::/32, 3fff::/20, and 2002::/16. Everything else is dropped, including IPv4-mapped and NAT64 forms, zones, names, and unknown kinds. The first 32 entries are looked at and at most eight kept. What is dialled is written again from the parsed bytes. An answer with no admitted address is treated as a failure.
- **`unavailable`** asks again after `retry_after_seconds`, kept between 30 s and 15 min.

## 5. One path at a time

A **session** is one endpoint and one connection for one accepted answer.

1. **Dial.** Bind an endpoint for the launch key, then dial the answer's endpoint ID at the admitted addresses with the ALPN, in one `connect` (bounds: 2 s and 1.5 s, proposals). The peer's identity is compared again after the handshake.
2. **Probe.** Open a stream, write the canonical CONNECT head, and read the instance's `200` (1 s, proposal). The probe's stream is then abandoned. Only now is `usable(generation)` reported.
3. **Refresh.** The answer is asked again at 80 % of its lifetime, so the admission is extended before the sidecar ends it. A newly accepted answer of the same generation and endpoint keeps the session and moves its deadline. A failed refresh moves nothing and is tried again after 15 s.

| Event | Session | Report | Next |
| --- | --- | --- | --- |
| The connection closes (idle timeout, admission ended, sidecar gone) | Ended | `lost` | Ask again, by the retry ladder |
| `pathDidFail` for the current tags | Probed; kept if the probe passes | `lost` only if the probe fails | Ladder after a failure; at most one such probe a second |
| A network change | Kept, not usable for new streams | `verifying`, at once and once | Probe after 0.5 s of quiet |
| That probe passes | Usable again; its streams were never touched | `usable`, same generation | Ask the rendezvous again |
| That probe fails | Ended | `lost` | Ask again at once |
| A refresh answers with another generation, or `unavailable` | Ended before anything new is dialled | `lost`, then `usable(new)` when the new path answers | Dial the new answer, or wait as told |
| The answer's lifetime passes with no newer answer accepted | Kept, not usable: no new stream, no probe | `verifying` | Keep asking. The instance closes the connection when the admission ends, which is the first row. |
| A newly accepted answer of the same generation, after that | Probed | `usable`, same generation, if the probe passes; `lost` if not | — |
| The lifetime passes while the answer is still being dialled | — | — | The dial is closed; ladder |
| A network change with no session | — | — | Whatever was under way stops; start over after 0.5 s |
| `deselect`, a new `select` | Ended | none | — |
| The App enters the background | Ended | `lost` if a session was up | Start over on the return, if still selected |

- **Retry ladder** (proposal): 0.25 s, 1 s, 5 s, 15 s, 60 s, then 5 min. The first step is short because an admission notice can lose the race with a fast dial. The count is cleared by a path that stays usable for 30 s, and by a network change or a return to the foreground, so a path that flaps backs off.
- **A page that is still loading** (`pageNotReady`) is asked again after 0.5 s, up to 40 times, before it counts as a failure.
- **`lost` is reported after the next attempt is arranged,** so the controller already sends new connections to the public origin when the report arrives.
- **The background** is handled by the transport itself as well as by the controller's `deselect`: no endpoint or socket outlives `didEnterBackground`.
- **An answer that runs out closes nothing by itself** (rule). The App stops using the path for anything new, and reports `verifying`, which closes no committed relay. Closing what the path carries on the phone's own timing would add nothing: the instance ends an expired admission's connection itself, and that arrives as `lost`.

## 6. Fencing

Every piece of work (a request, a dial, a probe, a session) carries a stamp, and a result whose stamp is no longer current changes nothing; whatever it produced is closed (rule).

`openStream(for:)` returns a stream only if, both before and after the binding's open:

- the tag's selection is the current one;
- the tag's rendezvous generation is the live session's;
- that session is usable, not being verified.

A stream that opens after any of these stopped holding is abandoned and the call throws, even if the same path is usable again by then: each time a path stops being usable the controller advances its path generation, so the tag the stream was asked under is stale. Ending a session cancels every stream opened on it.

**What owns each await, and what is checked after it.** Every state change happens on the main actor; each await below is the only place where state can move under an operation.

| Await | Owner, and what is asked where it starts | Checked again after it | Ends with | What may be left running |
| --- | --- | --- | --- | --- |
| The rendezvous script | The transport's one request task. Where it starts: still the current request. | The request's stamp; the lease against the clock; the WebView still shows the origin; inside the script, the controller refusal | The answer, a refusal, the script's 5 s, the task's 8 s, or anything that starts or cancels a request | The script, until its own deadline |
| `bind`, `connect` | The one dial task. Where each starts: the dial's authority. | The lease against the clock, then the dial's stamp | A result, its bound, or the dial's cancellation (`deselect`, a network change, the answer running out, the background) | The binding's call, until its endpoint is closed, which the dial does. A late endpoint or connection is closed. |
| A probe | The dial task, or the one probe task. Where its open, its CONNECT, and each read start: the dial's or the probe's authority. | The lease against the clock, then the probe's stamp | A result, its bound, or its cancellation | Its stream is abandoned either way |
| `closed()` | One watcher per session | The session's identifier | The connection closing, which ending the session causes | Nothing |
| `openStream` | The caller. Where the open starts: the path's authority, at the suspension count of the admission. | The lease against the clock; then the session's identifier, its authority at that count, and that it is usable | A stream, an error, or the caller's cancellation | The binding's open; a stream it still produces is abandoned |
| A stream's `read` and `write` | The call, which its start created; the caller only waits | The half's state, under the stream's lock: if `cancel()` came first the caller gets `cancelled` | A result, `cancel()`, or the caller's cancellation, which cancels the stream | The binding's call (section 3), which still records its outcome |
| A stream's `closeWrite` | The call, which owns the sending half from the start of `finish` | The same | The same | The binding's `finish`: it records `finished`, or resets the half if it fails |
| Timers | The transport | The lease's wake-up and the next-request timer: the lease against the clock, first. The debounce: through the probe it starts. Stability and probe spacing: nothing; they only keep counts. | Firing or cancellation | Nothing |

Terminal states:

- **A binding call (`DirectIrohCall`):** waiting, then done. Exactly one outcome reaches the caller; a later success goes to `discard`, a later failure nowhere.
- **A stream:** cancelled is final and idempotent. Its sending half ends as finished (clean, never reset) or abandoned (reset once). Its receiving half ends as ended (clean, never stopped) or abandoned (stopped once). Section 3 has the states between.
- **A session:** usable, or suspended (being verified after a network change, or its lease has run out), any number of times, then ended. Ended is final; a new path is a new session with a new endpoint.
- **A lease:** in force from its acceptance until the clock reaches its deadline, a newer answer replaces it, or its dial or session ends. Run out is final for that lease: only a newly accepted answer brings a new one.

**Path generations.** The controller issues them and the transport cannot know the current one. Within one selection it keeps the highest generation it has seen in a tag, and after each report that ends or suspends a path it refuses tags at or below that mark: the controller advances its generation on each such report, so those tags are stale. This catches only tags the transport has seen a successor of; the suspension count above is what makes a pending open safe, and the controller's own check at the commit point stays the authority.

The mark starts again at every `select`. The transport assumes only what the seam says, that a selection's path generations never go down, and nothing about how the controller numbers them from one selection to the next: a tag of another selection is refused because its selection is not the current one, whatever its number.

## 7. Tests, and what they do not show

In `ios-shell`, on the simulator:

| Invariant | Tests |
| --- | --- |
| Address classes, spellings, and every refused form | `DirectRendezvousTests.testOnlyPrivateIPv4UniqueLocalAndGlobalIPv6AddressesAreAdmitted` |
| An answer is accepted only for the selected instance and this launch; every rejection | `testAReadyAnswerForTheSelectedInstanceIsAccepted`, `testAnAnswerThatDoesNotMatchTheSelectionIsRejected` |
| An answer for another instance is no answer, `unavailable` included, and takes no path down | `testAnUnavailableAnswerForAnotherInstanceIsNoAnswer`, `DirectIrohTransportTests.testAnAnswerForAnotherInstanceNeitherBringsAPathUpNorTakesOneDown` |
| Lifetime; at most eight addresses; unknown entries skipped; `unavailable` | `testTheLifetimeIsTheAnswersOwnSpanAndAtMostFiveMinutes`, `testAtMostEightCheckedAddressesAreKeptAndTheRestOfTheListIsSkipped`, `testAnUnavailableAnswerSaysWhenToAskAgain` |
| The request is the instance's own (method, CSRF, cookie, Origin, body); refusals, redirects, size, and deadline; only on the instance's origin | `DirectRendezvousFetcherTests.testTheRequestIsTheInstancesOwnAndItsAnswerComesBackUnchanged`, `testNothingIsAskedBeforeTheInstancesDocumentAndItsCookieAreThere` |
| A service worker in control before, at, during, or at the end of the request is refused; this App's WebViews have no container | `testAServiceWorkerThatControlsTheDocumentAtAnyMomentIsRefused`, `testThisAppsWebViewsHaveNoServiceWorkerContainer` |
| A page cannot answer in the instance's place or move the request | `testAPageCannotAnswerInTheInstancesPlaceOrMoveTheRequest` |
| A cancelled caller leaves at once; late values are discarded, also when they lose to a deadline; nothing starts after cancellation; deadlines | `DirectIrohByteStreamTests`, first four tests |
| Bytes, end of stream, backpressure, half-close | `testBytesPassBothWaysAndTheEndIsNil`, `testAWriteReturnsOnlyOnceTheTransportAcceptedIt` |
| `cancel()` and task cancellation end calls in progress at once; the stream is over; only open halves are abandoned | `testCancelEndsAReadAndAWriteInProgressAtOnce`, `testACancelledCallerEndsItsReadAtOnceAndTheStreamWithIt`, `testCancelAbandonsOnlyTheHalvesThatHaveNotEnded`, `testAStreamReleasedWithoutCancelIsAbandonedNotFinished`, `testACallerThatIsAlreadyCancelledHandsNothingToTheBinding` |
| The sending half: handed over at `closeWrite` and never reset by a cancel; finished once; a failed `finish` resets it once, with or without a cancel; nothing after `closeWrite`; one call at a time | `testACancelDuringCloseWriteNeverResetsTheSendingHalf`, `testAFinishThatFailsAbandonsTheSendingHalfExactlyOnce`, `testTheSendingHalfTakesNothingAfterCloseWriteAndIsFinishedOnce`, `testOneReadAndOneWriteAtATime` |
| The receiving half: a clean end and a cancel are one decision, in both orders; a failed call abandons its half only | `testACleanEndAndACancelAreOneDecision`, `testAReadOrAWriteThatFailsAbandonsItsHalfAndLeavesTheOther` |
| Nothing is bound before an accepted `ready` answer | `DirectIrohTransportTests.testNothingExistsBeforeAnAcceptedReadyAnswer`, `testAHostThatIsNotADNSNameIsNeverAskedFor` |
| Dial, identity, probe, and only then `usable` | `testAReadyAnswerIsDialledProbedAndOnlyThenReportedUsable`, `testAnotherIdentityOrARefusedProbeIsClosedAndNeverUsable` |
| Bounds, `deselect`, and late results | `testADialThatDoesNotAnswerEndsAtItsBoundAndALateConnectionIsClosed`, `testDeselectEndsADialAtOnceAndWhatArrivesLateIsClosed`, `testAnAnswerThatArrivesAfterDeselectChangesNothing` |
| An answer is not used past its lifetime; only a newly accepted answer renews it; the span counts from the request; a late answer is not accepted | `testAnAnswerIsNotUsedPastItsLifetimeAndOnlyANewAnswerRenewsIt`, `testAnAnswerCountsFromWhenItWasAskedForAndOneThatArrivesTooLateIsNotAccepted`, `testAnAnswerThatRunsOutWhileItIsDialledIsGivenUp` |
| The clock decides, not the timer: with the clock past the deadline and the timer not fired, no stream, probe, dial result, probe result, or late open is taken, and a same-generation answer renews only through a probe; a timer that fires early changes nothing | `testALeaseTheClockHasPassedIsNotUsedAlthoughItsTimerHasNotFired`, `testAProbeThatEndsAfterTheLeaseRanOutReportsNothing`, `testAStreamThatOpensAfterTheLeaseRanOutIsAbandoned`, `testAnAnswerAfterTheClockPassedTheLeaseRenewsOnlyThroughAProbe` |
| No next stage starts after the deadline or a withdrawal: a late bind is followed by no connect, a late connect by no probe, a late probe open by no CONNECT; a probe or an open that was due starts nothing | `testNoStageOfADialStartsAfterItsLeaseRanOut`, `testNoStageOfADialStartsAfterItWasWithdrawn`, `testAProbeAndAnOpenThatWereDueStartNothingOnceTheLeaseRanOut` |
| An answer accepted during a dial replaces it, `unavailable` included; the old dial's result is closed; a refresh that fails during a dial ends nothing | `testAnAnswerAcceptedDuringADialReplacesItAndTheOldDialIsClosed`, `testAnUnavailableAnswerDuringADialEndsTheDial`, `testARefreshThatFailsDuringADialLeavesTheDialAndItsLease` |
| Path generations are compared within one selection only | `testPathGenerationsAreComparedWithinOneSelectionOnly` |
| Each other row of the table in section 5 | `testAClosedConnectionIsLostItsStreamsEndAndTheRendezvousIsAskedAgain`, `testANetworkChangeIsVerifyingThenUsableOnTheSamePathOrLost`, `testANetworkChangeBeforeAnyPathStartsOver`, `testAFailedCandidateProbesThePathAndOnlyADeadPathIsLost`, `testARefreshKeepsTheSameGenerationAndRetiresAReplacedOne`, `testFailuresBackOffAndAPageThatIsStillLoadingIsAskedAgainSoon` |
| Stream fencing and late opens | `testAStreamOpensOnlyUnderCurrentTags`, `testAStreamThatOpensAfterItsPathEndedIsAbandonedNotReturned`, `testAStreamThatOpensAcrossANetworkChangeOfItsPathIsAbandoned`, `testAStreamThatOpensAcrossTheRenewalOfItsAnswerIsAbandoned`, `testACancelledOpenEndsAtOnceAndItsLateStreamIsAbandoned` |
| Nothing lives in the background | `testNothingLivesInTheBackground`, `testATransportCreatedInTheBackgroundDoesNothingUntilTheForeground` |

**Not shown by any of this:**

- Anything about the real binding. The stand-ins copy the properties in section 2 from reading the source; no test here runs iroh. The engine over the generated types is compiled and tested for the first time with A1.
- Interoperability with the instance's sidecar, and the dead-path timings, which only the real sidecar's idle timeout produces.
- Devices: the Local Network prompt and its denial, real Wi-Fi, IPv6 and cellular, suspension, battery, and iOS 17; and that a WebView there has no service worker container either.
- A real service worker. The refusal is tested against a stand-in only (section 4).
- Every bound in `DirectIrohTiming`. They are proposals.

## 8. Still to implement and qualify

None of this is in this pull request, and none of it is done. Each item follows its dependency.

1. **After A1 links the gated binary:** `DirectIrohBinding`, the `DirectIrohEngine` over the generated types. A1 compiles the unmodified generated wrapper in its own Swift module, `IrohLib`; the adapter imports it and qualifies every generated name, so none enters the App's namespace. With the adapter:
   - tests with two real endpoints in one process over loopback: bind (no relay, the ID from the key equals `endpoint.id()`), a wrong identity, and the stream mapping;
   - **resource bounds against the real binding**, which the stand-ins cannot show: repeated cancellation of `connect`, `openBi`, `read`, and `writeAll`; a stream that opens late; both halves blocked on a live connection; and the end of a session. Each must show that the binding's leftover calls end and that their number is bounded. If a cancelled stream on a live connection cannot be bounded without closing the connection, that trade-off goes to a decision with these measurements; it is not accepted here.
   - the interoperability test against the published sidecar.
2. **After #3537 merges:**
   - `extension DirectIrohByteStream: DirectByteStream {}` and `extension DirectIrohTransport: DirectTransporting`, which maps `DirectTarget`, `DirectStreamTag`, and `DirectPathReport` to this file's types;
   - the rendezvous closure: the runtime's primary WebView for the target, only while that runtime is current and its store is the target's, passed to `DirectRendezvousFetcher`;
   - `NSLocalNetworkUsageDescription` in both locales;
   - the transport passed to `AppModel` by `AvibeApp`. This is the line that makes a build use a transport. It waits for its own approval and for the evidence above, and #3537's shipped-App test keeps passing until then.

## 9. Open

1. **D3 stays the iOS owner's.** This implements the first option (a fixed script in the runtime's WebView). It can run only while that WebView shows a document of the instance. A second hidden WebView on the instance origin would not depend on the page, at the cost of one more WebView per account.
2. **D4.** No preflight detects a denied Local Network permission: a denied dial fails like an unreachable address and the App stays on the public origin. Recognizing denial earlier would only save dials; it needs a device to design.
3. **Both halves busy** (section 3). The stock binding cannot end such a stream without closing its connection. No patched binding and no upstream change is authorized. The real-binding tests of section 8 measure it; the decision follows them.
4. **The sidecar closes a connection that has had no stream for five minutes.** An App left open on an idle page then sees `lost` and dials again. Whether to keep one idle stream instead is a measurement question.
