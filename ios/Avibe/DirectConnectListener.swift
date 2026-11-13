import Foundation
import Network

/// Bounds on the loopback CONNECT listener. The head limits are the
/// instance's own (docs/plans/remote-direct-ingress.md section 3). The time
/// bounds are proposed values for source and tests, not measurements
/// (docs/plans/remote-direct-ios.md section 6).
struct DirectConnectLimits: Equatable, Sendable {
    /// The largest request head from WebKit, and the largest answer head
    /// from the instance.
    let headBytes: Int
    /// How long WebKit has to finish its request head.
    let headSeconds: TimeInterval
    /// How long the transport has to open a stream.
    let openSeconds: TimeInterval
    /// How long the instance has to answer the CONNECT.
    let answerSeconds: TimeInterval
    /// Connections served at once, counted from accept to close.
    let maxConnections: Int
    /// After one side finishes sending, how long the other has to finish.
    let lingerSeconds: TimeInterval
    /// The most bytes one relay holds in memory per direction.
    let chunkBytes: Int
    /// The most bytes WebKit may send after its CONNECT head before a
    /// backend is selected. They are held, never forwarded early.
    let earlyBytes: Int
    /// How long a direct attempt runs alone before the public origin is
    /// also dialled.
    let hedgeSeconds: TimeInterval

    static let standard = DirectConnectLimits(
        headBytes: 4096,
        headSeconds: 5,
        openSeconds: 2,
        answerSeconds: 3,
        maxConnections: 64,
        lingerSeconds: 30,
        chunkBytes: 64 * 1024,
        earlyBytes: 16 * 1024,
        hedgeSeconds: 0.5
    )
}

/// WebKit's request head, as far as it has arrived.
enum DirectConnectRequest: Equatable {
    /// More bytes are needed.
    case incomplete
    /// A CONNECT for an allowed authority. `earlyData` followed the head.
    case accepted(DirectAuthority, earlyData: Data)
    /// The listener's own ownership check, carrying a nonce.
    case ownershipCheck(nonce: String)
    /// Refused with this HTTP status.
    case refused(Int)

    /// Accepts `CONNECT <host>:443 HTTP/1.1` for exactly one of
    /// `allowedHosts` and refuses everything else. `matchDomains` is a
    /// suffix match, so WebKit also sends this listener a CONNECT for any
    /// subdomain of a routed host.
    static func parse(
        _ received: Data,
        allowedHosts: Set<String>,
        limit: Int
    ) -> DirectConnectRequest {
        guard let end = DirectConnectWire.endOfHead(in: received) else {
            return received.count > limit ? .refused(400) : .incomplete
        }
        guard
            end.lowerBound - received.startIndex <= limit,
            let lines = DirectConnectWire.lines(of: received[..<end.lowerBound])
        else {
            return .refused(400)
        }
        let requestLine = lines[0].split(
            separator: " ",
            omittingEmptySubsequences: false
        )
        guard requestLine.count == 3, !requestLine[0].isEmpty else {
            return .refused(400)
        }
        if requestLine[0] == "OWNER", requestLine[2] == "HTTP/1.1", lines.count == 1 {
            return .ownershipCheck(nonce: String(requestLine[1]))
        }
        guard requestLine[0] == "CONNECT" else {
            return .refused(405)
        }
        guard requestLine[2] == "HTTP/1.1" else {
            return .refused(400)
        }

        var hosts: [String] = []
        for line in lines.dropFirst() {
            guard
                let colon = line.firstIndex(of: ":"),
                colon != line.startIndex,
                !line[..<colon].contains(where: { $0 == " " || $0 == "\t" })
            else {
                return .refused(400)
            }
            let name = line[..<colon].lowercased()
            if name == "content-length" || name == "transfer-encoding" {
                return .refused(400)
            }
            if name == "host" {
                hosts.append(
                    line[line.index(after: colon)...]
                        .trimmingCharacters(in: .whitespaces)
                        .lowercased()
                )
            }
        }
        guard hosts.count <= 1 else {
            return .refused(400)
        }
        // The target decides: exactly an allowed host, on port 443. Another
        // host, a subdomain, another port, no port, userinfo, and an IP
        // literal all fail here.
        let target = requestLine[1].lowercased()
        guard target.hasSuffix(":443") else {
            return .refused(403)
        }
        let host = String(target.dropLast(4))
        guard
            allowedHosts.contains(host),
            let authority = DirectAuthority(host: host)
        else {
            return .refused(403)
        }
        if let header = hosts.first,
           header != authority.host,
           header != authority.connectTarget
        {
            return .refused(400)
        }
        return .accepted(authority, earlyData: Data(received[end.upperBound...]))
    }
}

/// The instance's answer to the CONNECT, as far as it has arrived.
enum DirectConnectAnswer: Equatable {
    case incomplete
    /// `200`. `leftover` followed the head.
    case established(leftover: Data)
    /// Any other status.
    case refused(Int)
    /// Not an HTTP/1.1 response head, or one over the limit.
    case malformed

    static func parse(_ received: Data, limit: Int) -> DirectConnectAnswer {
        guard let end = DirectConnectWire.endOfHead(in: received) else {
            return received.count > limit ? .malformed : .incomplete
        }
        guard
            end.lowerBound - received.startIndex <= limit,
            let lines = DirectConnectWire.lines(of: received[..<end.lowerBound])
        else {
            return .malformed
        }
        let statusLine = lines[0].split(
            separator: " ",
            maxSplits: 2,
            omittingEmptySubsequences: false
        )
        guard
            statusLine.count >= 2,
            statusLine[0] == "HTTP/1.1",
            statusLine[1].utf8.count == 3,
            let status = Int(statusLine[1]),
            (100...599).contains(status)
        else {
            return .malformed
        }
        guard status == 200 else {
            return .refused(status)
        }
        return .established(leftover: Data(received[end.upperBound...]))
    }
}

enum DirectConnectWire {
    static func endOfHead(in data: Data) -> Range<Data.Index>? {
        data.range(of: Data("\r\n\r\n".utf8))
    }

    /// The head's lines, or `nil` when it is not printable ASCII.
    static func lines(of head: Data) -> [Substring]? {
        guard
            head.allSatisfy({ $0 == 0x0D || $0 == 0x0A || $0 == 0x09 || (0x20...0x7E).contains($0) }),
            let text = String(data: head, encoding: .ascii)
        else {
            return nil
        }
        var lines: [Substring] = []
        var rest = Substring(text)
        while let separator = rest.range(of: "\r\n") {
            lines.append(rest[..<separator.lowerBound])
            rest = rest[separator.upperBound...]
        }
        lines.append(rest)
        // A bare CR or LF inside a line is not a line ending.
        guard lines.allSatisfy({ !$0.contains("\r") && !$0.contains("\n") }) else {
            return nil
        }
        return lines
    }

    /// The head written to the instance: the ingress contract's canonical
    /// one, never the bytes WebKit sent.
    static func canonicalRequest(for authority: DirectAuthority) -> Data {
        Data(
            "CONNECT \(authority.connectTarget) HTTP/1.1\r\nHost: \(authority.connectTarget)\r\n\r\n".utf8
        )
    }

    static let established = Data(
        "HTTP/1.1 200 Connection Established\r\n\r\n".utf8
    )

    /// The listener's answer to its own ownership check.
    static func ownershipAnswer(nonce: String) -> Data {
        Data("HTTP/1.1 204 No Content\r\nX-Avibe-Owner: \(nonce)\r\n\r\n".utf8)
    }

    static func refusal(_ status: Int) -> Data {
        let reason: String
        switch status {
        case 400: reason = "Bad Request"
        case 403: reason = "Forbidden"
        case 405: reason = "Method Not Allowed"
        case 502: reason = "Bad Gateway"
        case 503: reason = "Service Unavailable"
        case 504: reason = "Gateway Timeout"
        default: reason = "Refused"
        }
        return Data(
            "HTTP/1.1 \(status) \(reason)\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".utf8
        )
    }
}

enum DirectConnectError: Error, Equatable {
    case timedOut
    case closedBeforeHead
    case refused(Int)
    case noAnswer
    /// WebKit sent more bytes before the tunnel than the early bound.
    case earlyOverflow
}

/// Runs `operation` with a deadline. The operation must honour cancellation
/// promptly; the call returns once it has. A value the operation produces
/// after the deadline already decided is handed to `discard`, so a stream
/// that arrives late is closed rather than leaked.
func directDeadline<Value: Sendable>(
    _ seconds: TimeInterval,
    discard: @escaping @Sendable (Value) -> Void = { _ in },
    _ operation: @escaping @Sendable () async throws -> Value
) async throws -> Value {
    let outcome: Result<Value, Error> = await withTaskGroup(
        of: Result<Value, Error>?.self
    ) { group in
        group.addTask {
            do {
                return .success(try await operation())
            } catch {
                return .failure(error)
            }
        }
        group.addTask {
            do {
                try await Task.sleep(for: .seconds(seconds))
            } catch {
                // Cancelled: the operation finished first.
                return nil
            }
            return .failure(DirectConnectError.timedOut)
        }
        var first: Result<Value, Error>?
        while let next = await group.next() {
            guard let result = next else {
                continue
            }
            if first == nil {
                first = result
                group.cancelAll()
            } else if case .success(let late) = result {
                discard(late)
            }
        }
        return first ?? .failure(DirectConnectError.timedOut)
    }
    return try outcome.get()
}

/// Which backend a committed relay runs on.
enum DirectRelayBackend: Equatable, Sendable {
    case direct
    case publicOrigin
}

/// The selection and account a plan was made under. A CONNECT whose plan
/// was fenced is planned again only within the same scope.
struct DirectPlanScope: Equatable, Sendable {
    let selection: Int
    let accountEpoch: Int

    /// Both numbers only grow in a process, so a scope with either one
    /// smaller is from before `other`.
    func isOlder(than other: DirectPlanScope) -> Bool {
        selection < other.selection || accountEpoch < other.accountEpoch
    }
}

/// What the path controller decides for one CONNECT the listener accepted
/// (docs/plans/remote-direct-ios.md section 6).
struct DirectConnectPlan: Sendable {
    let scope: DirectPlanScope
    /// The path generation the plan was made under. A relay committed under
    /// it is closed only by an event of a later generation.
    let generation: Int
    /// Opens a direct stream. `nil` when direct is not authorized for this
    /// CONNECT.
    let openDirect: (@Sendable () async throws -> any DirectByteStream)?
    /// Opens a connection to the host's public origin.
    let openPublic: @Sendable () async throws -> any DirectByteStream
    /// Whether the selection, lease and path generation this plan was made
    /// under are all still current. Checked when a candidate is ready and
    /// again just before the commit point.
    let isCurrent: @Sendable () async -> Bool
    /// The direct candidate failed before anything was forwarded.
    let directFailed: @Sendable () -> Void
}

/// Decides each accepted CONNECT. `nil` refuses it.
typealias DirectConnectPlanner = @Sendable (DirectAuthority) async -> DirectConnectPlan?

/// The App's loopback HTTP CONNECT proxy. There is one per App process, and
/// every route the App writes points at its port. WebKit reaches it through
/// the account data stores' `proxyConfigurations`. For each CONNECT it relays
/// opaque bytes to a direct stream or to the host's public origin; it never
/// reads or terminates TLS.
actor DirectConnectListener {
    private struct Relay {
        /// Which came first: the relay ending on its own, or a close.
        let claim: DirectRelayClaim
        let task: Task<Void, Never>
        /// The relay's current plan, once it has one: its scope, its path
        /// generation, and its race, which a newer generation retires.
        var plan: (scope: DirectPlanScope, generation: Int, race: DirectRaceHandle)?
        /// Set at the commit point; until then the relay is a candidate.
        var committed: (host: String, backend: DirectRelayBackend, generation: Int)?
    }

    /// What `planned` decided for a relay's new plan.
    enum PlanAdmission: Equatable {
        case admitted
        /// Made under a path generation already retired: it is not raced.
        case obsolete
        /// Made under a scope already retired, or the relay is gone.
        case refused
    }

    private let limits: DirectConnectLimits
    private let planner: DirectConnectPlanner
    /// Called with whether a route had been published to this listener
    /// before its socket failed (`DirectSocketHealth`).
    private let onListenerFailure: @Sendable (_ routed: Bool) -> Void
    private let queue = DispatchQueue(label: "bot.avibe.ios.direct-connect")
    /// Made once per listener. Only this listener's accept loop answers an
    /// ownership check that carries it.
    let ownershipNonce: String
    /// Whether the current socket is serving, readable at once from any
    /// thread: its failure is recorded where Network reports it, before the
    /// failure callback reaches anyone.
    let health = DirectSocketHealth()
    private var listener: NWListener?
    private var allowedHosts: Set<String> = []
    private var relays: [UUID: Relay] = [:]
    private var isStopped = false
    /// The close of the previous listening socket, until it has finished.
    private var closing: Task<Void, Never>?
    /// The one connection beyond the limit, kept for the ownership check.
    /// Recorded from admission until its task has closed its connection.
    private var spare: (id: UUID, task: Task<Void, Never>)?
    /// The bind whose socket admits connections; `nil` once it began to
    /// close.
    private var acceptingGeneration: Int?
    /// How long the spare connection waits for a request head. Far shorter
    /// than an ownership check's window, which retries: a slow or
    /// incomplete request cannot hold the spare through a check.
    static let spareHeadSeconds: TimeInterval = 0.5
    /// The highest path generation and the newest scope retired so far.
    private var retiredGeneration = Int.min
    private var retiredScope = DirectPlanScope(selection: .min, accountEpoch: .min)
    private var pendingBind: DirectOnceAnswer<UInt16>?
    /// Advances with every bind.
    private var bindGeneration = 0

    init(
        limits: DirectConnectLimits = .standard,
        ownershipNonce: String = UUID().uuidString,
        planner: @escaping DirectConnectPlanner,
        onListenerFailure: @escaping @Sendable (_ routed: Bool) -> Void
    ) {
        self.limits = limits
        self.ownershipNonce = ownershipNonce
        self.planner = planner
        self.onListenerFailure = onListenerFailure
    }

    /// Adds one host to those a CONNECT may name.
    func allowHost(_ host: String) {
        allowedHosts.insert(host)
    }

    /// Binds `127.0.0.1` on exactly `port`, or on a port the system chooses
    /// when `port` is `nil`, and returns the bound port. The socket does not
    /// share its address (`allowLocalEndpointReuse` is off), so a port held by
    /// anyone else fails the bind.
    func bind(port requested: UInt16?) async throws -> UInt16 {
        // A listening socket still closing, or a spare it accepted, holds
        // the port.
        await closing?.value
        if listener == nil {
            await drainSpare()
        }
        guard !isStopped, listener == nil else {
            throw CancellationError()
        }
        let endpointPort: NWEndpoint.Port
        if let requested {
            guard let exact = NWEndpoint.Port(rawValue: requested) else {
                throw CancellationError()
            }
            endpointPort = exact
        } else {
            endpointPort = .any
        }
        let parameters = NWParameters.tcp
        parameters.allowLocalEndpointReuse = false
        parameters.requiredLocalEndpoint = .hostPort(
            host: "127.0.0.1",
            port: endpointPort
        )
        // An exact port is given once, in the required local endpoint:
        // passing it to `NWListener` as well fails with EINVAL ("Local
        // endpoint has port set, cannot override").
        let listener: NWListener
        if requested == nil {
            listener = try NWListener(using: parameters, on: .any)
        } else {
            listener = try NWListener(using: parameters)
        }
        self.listener = listener
        // Each bind has its own generation, so a failure reported by an
        // earlier socket of this actor is recognised as stale, and so is a
        // connection that socket accepted after it began closing.
        bindGeneration += 1
        let generation = bindGeneration
        acceptingGeneration = generation
        listener.newConnectionHandler = { [weak self, queue] connection in
            guard let self else {
                connection.cancel()
                return
            }
            // Keep this start inside the handler, and keep `acceptLocalOnly`
            // off the parameters above. With the connection started after
            // the handler returned and that flag set, the iOS simulator
            // reset every accepted connection. Which of the two caused it
            // was not established; macOS showed neither.
            connection.start(queue: queue)
            Task {
                await self.accept(connection, bindGeneration: generation)
            }
        }
        let health = health
        // Resolved by the socket's state, by cancellation of this task, or
        // by `closeListeningSocket()` and `stop()`, which replace the state
        // handler: a pending bind always returns.
        let answer = DirectOnceAnswer<UInt16>()
        pendingBind = answer
        listener.stateUpdateHandler = { [weak self, weak listener] state in
            switch state {
            case .ready:
                // From here on, a state change is the socket failing.
                listener?.stateUpdateHandler = { [weak self] state in
                    if case .failed = state {
                        let routed = health.failed(generation: generation)
                        Task {
                            await self?.socketDidFail(bindGeneration: generation, routed: routed)
                        }
                    }
                }
                if let port = listener?.port?.rawValue {
                    health.serving(generation: generation)
                    answer.resolve(with: .success(port))
                } else {
                    answer.resolve(with: .failure(CancellationError()))
                }
            case .failed(let error), .waiting(let error):
                listener?.cancel()
                answer.resolve(with: .failure(error))
            case .cancelled:
                answer.resolve(with: .failure(CancellationError()))
            default:
                break
            }
        }
        listener.start(queue: queue)
        let bound: UInt16
        do {
            bound = try await withTaskCancellationHandler {
                try await withCheckedThrowingContinuation { continuation in
                    answer.wait(with: continuation)
                }
            } onCancel: {
                listener.cancel()
                answer.resolve(with: .failure(CancellationError()))
            }
        } catch {
            if pendingBind === answer {
                pendingBind = nil
            }
            if self.listener === listener {
                self.listener = nil
            }
            if acceptingGeneration == generation {
                acceptingGeneration = nil
            }
            listener.cancel()
            throw error
        }
        if pendingBind === answer {
            pendingBind = nil
        }
        if self.listener !== listener {
            // Closed or stopped while binding.
            listener.cancel()
            throw CancellationError()
        }
        return bound
    }

    /// Closes the listening socket and keeps every accepted connection
    /// (B2 on background). Returns once the socket has closed, or after a
    /// second; `bind(port:)` also waits for it. Cancelling an `NWListener`
    /// closes its socket later: a bind of the same port straight after
    /// `cancel()` failed with EADDRINUSE on the simulator. A bind still
    /// pending ends with `CancellationError`.
    func closeListeningSocket() async {
        // Before any suspension: no connection is admitted from here on.
        acceptingGeneration = nil
        endPendingBind()
        health.closed()
        if let listener {
            self.listener = nil
            closing = Task {
                await Self.cancelAndWait(listener)
            }
        }
        // The spare belongs to the socket that accepted it. It is cancelled
        // and waited for, through its own completion, before this returns,
        // so a same-port bind never meets it.
        await drainSpare()
        await closing?.value
    }

    private static func cancelAndWait(_ listener: NWListener) async {
        let closed = DirectOnceAnswer<Void>()
        listener.stateUpdateHandler = { state in
            if case .cancelled = state {
                closed.resolve(with: .success(()))
            }
        }
        listener.cancel()
        _ = try? await directDeadline(1) {
            try await withTaskCancellationHandler {
                try await withCheckedThrowingContinuation { continuation in
                    closed.wait(with: continuation)
                }
            } onCancel: {
                closed.resolve(with: .failure(CancellationError()))
            }
        }
    }

    /// Stops for good: the listening socket and every connection.
    func stop() {
        guard !isStopped else {
            return
        }
        isStopped = true
        acceptingGeneration = nil
        endPendingBind()
        health.closed()
        listener?.stateUpdateHandler = nil
        listener?.cancel()
        listener = nil
        for relay in relays.values {
            relay.task.cancel()
        }
        relays.removeAll()
        endSpare()
    }

    /// Closes the committed relays on `backend` (`nil`: both) whose plan was
    /// made under a path generation before `generation`, and returns, per
    /// host, how many it closed. A relay committed under a later plan,
    /// after the event that asked for this close, is kept. Relays not yet
    /// committed are left to their plan's fence. A relay this listener did
    /// not accept is never touched.
    func closeRelays(
        backend: DirectRelayBackend?,
        before generation: Int = .max
    ) -> [String: Int] {
        var closed: [String: Int] = [:]
        for (id, relay) in relays {
            guard
                let committed = relay.committed,
                backend == nil || committed.backend == backend,
                committed.generation < generation
            else {
                continue
            }
            // Counted only if this close ends it; one that already ended on
            // its own, and has not been removed yet, is not this event's.
            if relay.claim.closeByEvent() {
                closed[committed.host, default: 0] += 1
            }
            relay.task.cancel()
            relays.removeValue(forKey: id)
        }
        return closed
    }

    /// Retires uncommitted work made before an event: a relay whose plan's
    /// scope is older than `scope` ends (a new selection or account never
    /// takes over a CONNECT planned for the old one), and a relay whose plan's
    /// path generation is before `generation` has its race cancelled and is
    /// planned again. Committed relays, relays still reading their head, and
    /// work planned at or after the event are untouched. The marks only grow,
    /// so a retirement that runs late cannot touch newer work, and a plan
    /// registered after it is checked against them (`planned`).
    func retireUncommitted(before generation: Int, outside scope: DirectPlanScope) {
        retiredGeneration = max(retiredGeneration, generation)
        if retiredScope.isOlder(than: scope) {
            retiredScope = scope
        }
        for (id, relay) in relays where relay.committed == nil {
            guard let plan = relay.plan else {
                continue
            }
            if plan.scope.isOlder(than: retiredScope) {
                relay.task.cancel()
                relays.removeValue(forKey: id)
            } else if plan.generation < retiredGeneration {
                plan.race.retire()
            }
        }
    }

    private func endPendingBind() {
        pendingBind?.resolve(with: .failure(CancellationError()))
        pendingBind = nil
    }

    /// The socket of bind `bindGeneration` failed. Reported only while that
    /// socket is the current one: a failure of an earlier socket of this
    /// listener, closed for the background and replaced, is stale.
    func socketDidFail(bindGeneration generation: Int, routed: Bool) {
        guard !isStopped, listener != nil, generation == bindGeneration else {
            return
        }
        onListenerFailure(routed)
    }

    private func accept(_ connection: NWConnection, bindGeneration generation: Int) {
        // Admission ends when the socket that accepted the connection begins
        // to close, fails, or is rejected (its ownership check failed): an
        // accept queued before that, delivered after it, is not admitted, as
        // a relay or as the spare. A failure is recorded in `health` where
        // Network reports it, and a rejection by the controller before it
        // schedules the stop, so neither waits for this actor.
        guard
            !isStopped,
            generation == acceptingGeneration,
            health.isServing
        else {
            connection.cancel()
            return
        }
        guard relays.count < limits.maxConnections else {
            // One spare connection beyond the limit is for the listener's
            // own ownership check, so relays the App keeps cannot starve a
            // re-bind's check. It reads a head, answers only a check with
            // this listener's nonce, and closes; anything else, and any
            // connection while the spare is in use, is closed with no
            // answer. The limit plus one bounds what this listener holds.
            guard spare == nil else {
                connection.cancel()
                return
            }
            let nonce = ownershipNonce
            let id = UUID()
            spare = (id, Task { [weak self] in
                await Self.answerOwnershipCheckOnly(connection, nonce: nonce)
                connection.cancel()
                await self?.spareDidEnd(id)
            })
            return
        }
        let id = UUID()
        let limits = limits
        let planner = planner
        let allowedHosts = allowedHosts
        let nonce = ownershipNonce
        let claim = DirectRelayClaim()
        relays[id] = Relay(claim: claim, task: Task { [weak self] in
            await Self.serve(
                connection,
                id: id,
                listener: self,
                allowedHosts: allowedHosts,
                ownershipNonce: nonce,
                limits: limits,
                planner: planner
            )
            // Linearized with `closeRelays`, before anything else: from here
            // a close does not count this relay, however long the actor
            // takes to hear `relayDidEnd`.
            claim.end()
            connection.cancel()
            await self?.relayDidEnd(id)
        })
    }

    /// Clears the spare only if it is still this one. A cancelled spare stays
    /// recorded until here, so a newer one cannot take its place first; the
    /// id fence is a second guard, not the cleanup.
    private func spareDidEnd(_ id: UUID) {
        if spare?.id == id {
            spare = nil
        }
    }

    /// Cancels the spare without forgetting it: it stays recorded, and
    /// counted against the bound, until its own completion clears it.
    private func endSpare() {
        spare?.task.cancel()
    }

    /// Cancels the spare and waits until its connection is closed and it
    /// has cleared itself. Waiting on its task lets this actor run the
    /// spare's own `spareDidEnd`, so this cannot deadlock.
    private func drainSpare() async {
        guard let spare else {
            return
        }
        spare.task.cancel()
        await spare.task.value
    }

    /// A relay's task ended, on its own or because a close cancelled it.
    /// A relay whose stream failed before any event closed it is not counted
    /// by a later event: that event did not close it, and the page already
    /// saw that connection fail.
    private func relayDidEnd(_ id: UUID) {
        relays.removeValue(forKey: id)
    }

    /// Records a relay's new plan, and checks it against what was retired
    /// before it got here.
    private func planned(
        _ id: UUID,
        scope: DirectPlanScope,
        generation: Int,
        race: DirectRaceHandle
    ) -> PlanAdmission {
        guard !isStopped, relays[id] != nil, !scope.isOlder(than: retiredScope) else {
            return .refused
        }
        relays[id]?.plan = (scope, generation, race)
        return generation < retiredGeneration ? .obsolete : .admitted
    }

    /// Records the commit point. `false` when the relay was closed meanwhile
    /// (an account transition, a stop), so nothing may be forwarded.
    private func commit(
        _ id: UUID,
        host: String,
        backend: DirectRelayBackend,
        generation: Int
    ) -> Bool {
        guard !isStopped, relays[id] != nil else {
            return false
        }
        relays[id]?.committed = (host, backend, generation)
        return true
    }

    private func uncommit(_ id: UUID) {
        relays[id]?.committed = nil
    }

    // Everything below runs off the actor: one task per connection, so
    // copying bytes for one connection never waits for another.

    private enum RaceOutcome {
        case won(DirectRelayBackend, any DirectByteStream, leftover: Data)
        /// A candidate was ready but its plan was no longer current.
        case fenced
        case failed(status: Int)
    }

    /// The spare connection: answers this listener's ownership check and
    /// nothing else, within the head bound.
    private static func answerOwnershipCheckOnly(
        _ connection: NWConnection,
        nonce: String
    ) async {
        let request = try? await directDeadline(spareHeadSeconds) {
            try await readRequest(
                from: connection,
                allowedHosts: [],
                limits: .standard
            )
        }
        guard request == .ownershipCheck(nonce: nonce) else {
            return
        }
        _ = try? await directDeadline(0.25) {
            try await connection.sendAll(DirectConnectWire.ownershipAnswer(nonce: nonce))
            try await connection.finishSending()
        }
    }

    private static func serve(
        _ connection: NWConnection,
        id: UUID,
        listener: DirectConnectListener?,
        allowedHosts: Set<String>,
        ownershipNonce: String,
        limits: DirectConnectLimits,
        planner: DirectConnectPlanner
    ) async {
        let authority: DirectAuthority
        let earlyData: Data
        do {
            let request = try await directDeadline(limits.headSeconds) {
                try await readRequest(
                    from: connection,
                    allowedHosts: allowedHosts,
                    limits: limits
                )
            }
            switch request {
            case .accepted(let accepted, let data):
                authority = accepted
                earlyData = data
            case .ownershipCheck(let nonce):
                if nonce == ownershipNonce {
                    _ = try? await directDeadline(0.25) {
                        try await connection.sendAll(
                            DirectConnectWire.ownershipAnswer(nonce: nonce)
                        )
                        try await connection.finishSending()
                    }
                } else {
                    await refuse(connection, status: 405, within: limits.headSeconds)
                }
                return
            case .refused(let status):
                await refuse(connection, status: status, within: limits.headSeconds)
                return
            case .incomplete:
                return
            }
        } catch {
            // A head that never completes, or a client that went away, is
            // closed without an answer.
            return
        }
        // Bytes sent ahead of the answer are held, never forwarded before a
        // backend is selected, and bounded.
        guard earlyData.count <= limits.earlyBytes else {
            return
        }
        // From here on every read of WebKit's side goes through the reader,
        // which keeps and bounds what WebKit sends before the tunnel.
        let reader = DirectClientReader(
            connection,
            maximumLength: limits.chunkBytes,
            earlyLimit: limits.earlyBytes,
            initial: earlyData
        )
        reader.start()

        // One fresh plan if the first one is fenced before its commit point
        // (the path generation changed while it raced), within the same
        // selection and account only.
        var scope: DirectPlanScope?
        for _ in 0..<2 {
            let race = DirectRaceHandle()
            guard
                !Task.isCancelled,
                let plan = await planner(authority),
                scope == nil || plan.scope == scope,
                let listener
            else {
                if !Task.isCancelled {
                    await answerRefusal(connection, status: scope == nil ? 403 : 503)
                    await awaitClose(reader, within: limits.headSeconds)
                }
                return
            }
            let admission = await listener.planned(
                id,
                scope: plan.scope,
                generation: plan.generation,
                race: race
            )
            guard admission != .refused else {
                if !Task.isCancelled {
                    await answerRefusal(connection, status: 503)
                    await awaitClose(reader, within: limits.headSeconds)
                }
                return
            }
            scope = plan.scope
            let outcome: RaceOutcome
            if admission == .obsolete {
                outcome = .fenced
            } else {
                // The race runs as its own task, so a newer path generation
                // can cancel it without ending the CONNECT.
                let raceTask = Task {
                    await raceWatchingClient(
                        plan,
                        authority: authority,
                        limits: limits,
                        reader: reader
                    )
                }
                race.attach { raceTask.cancel() }
                let watched = await withTaskCancellationHandler {
                    await raceTask.value
                } onCancel: {
                    raceTask.cancel()
                }
                guard let watched else {
                    // WebKit closed its side, or sent too much, before a
                    // backend was chosen.
                    return
                }
                if race.isRetired {
                    if case .won(_, let stream, _) = watched {
                        stream.cancel()
                    }
                    outcome = .fenced
                } else {
                    outcome = watched
                }
            }
            switch outcome {
            case .fenced:
                continue
            case .failed(let status):
                guard !Task.isCancelled else {
                    return
                }
                await answerRefusal(connection, status: status)
                await awaitClose(reader, within: limits.headSeconds)
                return
            case .won(let backend, let stream, let leftover):
                // The commit point. Registered first, so an event that comes
                // after the check below finds this relay and closes it.
                guard
                    await listener.commit(
                        id,
                        host: authority.host,
                        backend: backend,
                        generation: plan.generation
                    )
                else {
                    stream.cancel()
                    return
                }
                guard await plan.isCurrent() else {
                    await listener.uncommit(id)
                    stream.cancel()
                    continue
                }
                defer {
                    stream.cancel()
                }
                // WebKit's side must still be there and within the early
                // bound; from here its bytes are the tunnel's.
                guard reader.startTunnel() else {
                    return
                }
                do {
                    try await connection.sendAll(DirectConnectWire.established)
                    try await relay(
                        reader,
                        to: connection,
                        stream,
                        leftover: leftover,
                        limits: limits
                    )
                } catch {
                    // Either side ended the tunnel, or the relay was closed.
                    // A tunnel is never redialled and nothing is replayed.
                }
                return
            }
        }
        await answerRefusal(connection, status: 503)
        await awaitClose(reader, within: limits.headSeconds)
    }

    private enum WatchedRace {
        case race(RaceOutcome)
        /// WebKit's side ended, or sent more than the early bound.
        case clientGone
        /// The watch was stopped because the race ended.
        case stopped
    }

    /// Races the plan's candidates while watching WebKit's side. If WebKit
    /// closes its side, or sends more than the early bound, before a backend
    /// is chosen, the race is cancelled and a stream it produced is closed:
    /// `nil`. The reader keeps what WebKit sends meanwhile.
    private static func raceWatchingClient(
        _ plan: DirectConnectPlan,
        authority: DirectAuthority,
        limits: DirectConnectLimits,
        reader: DirectClientReader
    ) async -> RaceOutcome? {
        await withTaskGroup(of: WatchedRace.self) { group in
            group.addTask {
                .race(await race(plan, authority: authority, limits: limits))
            }
            group.addTask {
                do {
                    try await reader.waitUntilGone()
                    return .clientGone
                } catch {
                    return .stopped
                }
            }
            var outcome: RaceOutcome?
            var clientGone = false
            while let next = await group.next() {
                switch next {
                case .race(let result):
                    if clientGone {
                        if case .won(_, let stream, _) = result {
                            stream.cancel()
                        }
                    } else {
                        outcome = result
                        group.cancelAll()
                    }
                case .clientGone:
                    clientGone = true
                    if case .won(_, let stream, _) = outcome {
                        stream.cancel()
                    }
                    outcome = nil
                    group.cancelAll()
                case .stopped:
                    break
                }
            }
            return clientGone ? nil : outcome
        }
    }

    private enum Candidate {
        case direct
        case publicOrigin
    }

    private enum CandidateResult {
        case ready(Candidate, any DirectByteStream, Data)
        case failed(Candidate, Error)
    }

    /// Races the candidates of one plan. Only this function selects a winner:
    /// the first ready candidate whose plan is still current. Every other
    /// candidate is cancelled, and one that becomes ready anyway is closed.
    /// No byte from WebKit has been sent on any of them.
    private static func race(
        _ plan: DirectConnectPlan,
        authority: DirectAuthority,
        limits: DirectConnectLimits
    ) async -> RaceOutcome {
        let directAttempted = plan.openDirect != nil
        let directSettled = DirectSettledFlag()
        return await withTaskGroup(of: CandidateResult.self) { group in
            if let openDirect = plan.openDirect {
                group.addTask {
                    do {
                        let stream = try await directDeadline(
                            limits.openSeconds,
                            discard: { $0.cancel() }
                        ) {
                            try await openDirect()
                        }
                        do {
                            let leftover = try await directDeadline(limits.answerSeconds) {
                                try await write(
                                    DirectConnectWire.canonicalRequest(for: authority),
                                    to: stream
                                )
                                return try await readAnswer(from: stream, limits: limits)
                            }
                            return .ready(.direct, stream, leftover)
                        } catch {
                            stream.cancel()
                            throw error
                        }
                    } catch {
                        // Lets the public candidate start now instead of
                        // waiting out the hedge.
                        directSettled.set()
                        return .failed(.direct, error)
                    }
                }
            }
            group.addTask {
                if directAttempted {
                    // The hedge: the public origin is dialled when the direct
                    // attempt has had its head start, or as soon as it fails.
                    let deadline = ContinuousClock.now.advanced(by: .seconds(limits.hedgeSeconds))
                    while !directSettled.isSet, ContinuousClock.now < deadline {
                        do {
                            try await Task.sleep(for: .milliseconds(25))
                        } catch {
                            return .failed(.publicOrigin, CancellationError())
                        }
                    }
                }
                guard !Task.isCancelled else {
                    return .failed(.publicOrigin, CancellationError())
                }
                do {
                    let stream = try await plan.openPublic()
                    return .ready(.publicOrigin, stream, Data())
                } catch {
                    return .failed(.publicOrigin, error)
                }
            }

            var winner: (DirectRelayBackend, any DirectByteStream, Data)?
            var fenced = false
            var directRefusal: Int?
            var directTimedOut = false
            while let result = await group.next() {
                switch result {
                case .ready(let candidate, let stream, let leftover):
                    guard winner == nil, !Task.isCancelled else {
                        stream.cancel()
                        continue
                    }
                    if await plan.isCurrent() {
                        winner = (candidate == .direct ? .direct : .publicOrigin, stream, leftover)
                        group.cancelAll()
                    } else {
                        // Fenced: every candidate of this plan is invalid.
                        stream.cancel()
                        fenced = true
                        group.cancelAll()
                    }
                case .failed(let candidate, let error):
                    guard
                        candidate == .direct,
                        winner == nil,
                        !fenced,
                        // A race cancelled because WebKit went away is no
                        // failure of the path.
                        !Task.isCancelled,
                        !(error is CancellationError)
                    else {
                        continue
                    }
                    if case DirectConnectError.refused(let status) = error {
                        directRefusal = status
                    } else if case DirectConnectError.timedOut = error {
                        directTimedOut = true
                    }
                    plan.directFailed()
                }
            }
            if let winner {
                return .won(winner.0, winner.1, leftover: winner.2)
            }
            if fenced {
                return .fenced
            }
            if let directRefusal, (400...599).contains(directRefusal) {
                return .failed(status: directRefusal)
            }
            return .failed(status: directTimedOut ? 504 : 502)
        }
    }

    private static func readRequest(
        from connection: NWConnection,
        allowedHosts: Set<String>,
        limits: DirectConnectLimits
    ) async throws -> DirectConnectRequest {
        var received = Data()
        while true {
            let chunk = try await connection.receiveChunk(
                maximumLength: limits.chunkBytes
            )
            received.append(chunk.data)
            let request = DirectConnectRequest.parse(
                received,
                allowedHosts: allowedHosts,
                limit: limits.headBytes
            )
            if request != .incomplete {
                return request
            }
            if chunk.isComplete {
                throw DirectConnectError.closedBeforeHead
            }
        }
    }

    private static func readAnswer(
        from stream: any DirectByteStream,
        limits: DirectConnectLimits
    ) async throws -> Data {
        var received = Data()
        while true {
            guard
                let data = try await read(
                    from: stream,
                    maxLength: limits.chunkBytes
                )
            else {
                throw DirectConnectError.noAnswer
            }
            received.append(data)
            switch DirectConnectAnswer.parse(received, limit: limits.headBytes) {
            case .incomplete:
                continue
            case .established(let leftover):
                return leftover
            case .refused(let status):
                throw DirectConnectError.refused(status)
            case .malformed:
                throw DirectConnectError.noAnswer
            }
        }
    }

    /// Copies both directions until both have finished. Each direction reads
    /// its next chunk only after the previous one was accepted, so a slow
    /// reader on one side slows the sender on the other.
    private static func relay(
        _ reader: DirectClientReader,
        to connection: NWConnection,
        _ stream: any DirectByteStream,
        leftover: Data,
        limits: DirectConnectLimits
    ) async throws {
        try await withThrowingTaskGroup(of: Void.self) { group in
            defer {
                group.cancelAll()
            }
            group.addTask {
                // The reader holds WebKit's early bytes first, then the rest.
                while let data = try await reader.read() {
                    try await write(data, to: stream)
                }
                try await closeWrite(of: stream)
            }
            group.addTask {
                var pending = leftover
                while true {
                    if !pending.isEmpty {
                        try await connection.sendAll(pending)
                    }
                    guard
                        let data = try await read(
                            from: stream,
                            maxLength: limits.chunkBytes
                        )
                    else {
                        try await connection.finishSending()
                        return
                    }
                    pending = data
                }
            }
            // One side has finished sending. The other direction keeps
            // going, for a bounded time.
            _ = try await group.next()
            group.addTask {
                try await Task.sleep(for: .seconds(limits.lingerSeconds))
                throw DirectConnectError.timedOut
            }
            _ = try await group.next()
        }
    }

    /// Answers a request that will not be tunnelled, then gives the client
    /// `seconds` to read the answer and close.
    private static func refuse(
        _ connection: NWConnection,
        status: Int,
        within seconds: TimeInterval
    ) async {
        await answerRefusal(connection, status: status)
        await awaitClose(connection, within: seconds)
    }

    /// Writes the refusal and finishes sending. A short write to a socket
    /// that was just accepted; bounded in case the client is not reading.
    private static func answerRefusal(
        _ connection: NWConnection,
        status: Int
    ) async {
        _ = try? await directDeadline(0.25) {
            try await connection.sendAll(DirectConnectWire.refusal(status))
            try await connection.finishSending()
        }
    }

    /// The same, once the reader owns WebKit's side.
    private static func awaitClose(
        _ reader: DirectClientReader,
        within seconds: TimeInterval
    ) async {
        _ = try? await directDeadline(seconds) {
            while try await reader.read() != nil {}
        }
    }

    /// Gives the client `seconds` to read the answer and close. Closing
    /// first, with bytes of the client's still unread, would reset the
    /// connection and could cost it the answer.
    private static func awaitClose(
        _ connection: NWConnection,
        within seconds: TimeInterval
    ) async {
        _ = try? await directDeadline(seconds) {
            while try await !connection.receiveChunk(maximumLength: 4096).isComplete {}
        }
    }

    // A cancelled task ends its stream, so a stream that does not watch for
    // cancellation itself still stops.

    private static func read(
        from stream: any DirectByteStream,
        maxLength: Int
    ) async throws -> Data? {
        try await withTaskCancellationHandler {
            try await stream.read(maxLength: maxLength)
        } onCancel: {
            stream.cancel()
        }
    }

    private static func write(
        _ data: Data,
        to stream: any DirectByteStream
    ) async throws {
        try await withTaskCancellationHandler {
            try await stream.write(data)
        } onCancel: {
            stream.cancel()
        }
    }

    private static func closeWrite(
        of stream: any DirectByteStream
    ) async throws {
        try await withTaskCancellationHandler {
            try await stream.closeWrite()
        } onCancel: {
            stream.cancel()
        }
    }

    /// Connects to `port` on the loopback address and asks the listener
    /// there for `nonce`. True only when this listener's own accept loop
    /// answered. A liveness and correlation check for the App's own port,
    /// not authentication against a hostile local process.
    static func ownershipCheck(
        port: UInt16,
        nonce: String,
        within seconds: TimeInterval
    ) async -> Bool {
        // Tried again until the window ends: one attempt can find the spare
        // connection briefly taken (`spareHeadSeconds`).
        let deadline = ContinuousClock.now.advanced(by: .seconds(seconds))
        while ContinuousClock.now < deadline, !Task.isCancelled {
            let remaining = ContinuousClock.now.duration(to: deadline)
            let remainingSeconds = Double(remaining.components.seconds)
                + Double(remaining.components.attoseconds) / 1e18
            if await checkOwnershipOnce(port: port, nonce: nonce, within: remainingSeconds) {
                return true
            }
            // Never past the window: the pause is the lesser of 50 ms and
            // what remains.
            let left = ContinuousClock.now.duration(to: deadline)
            guard left > .zero else {
                break
            }
            try? await Task.sleep(for: min(.milliseconds(50), left))
        }
        return false
    }

    private static func checkOwnershipOnce(
        port: UInt16,
        nonce: String,
        within seconds: TimeInterval
    ) async -> Bool {
        guard seconds > 0, let endpointPort = NWEndpoint.Port(rawValue: port) else {
            return false
        }
        let connection = NWConnection(
            host: "127.0.0.1",
            port: endpointPort,
            using: .tcp
        )
        let stream = NWConnectionByteStream(connection: connection)
        defer {
            stream.cancel()
        }
        do {
            return try await directDeadline(seconds) {
                try await stream.connect()
                try await stream.write(Data("OWNER \(nonce) HTTP/1.1\r\n\r\n".utf8))
                var received = Data()
                while let data = try await stream.read(maxLength: 1024) {
                    received.append(data)
                    if received.count > 1024 {
                        break
                    }
                }
                return received == DirectConnectWire.ownershipAnswer(nonce: nonce)
            }
        } catch {
            return false
        }
    }
}

/// Set once the direct candidate has settled, either way.
private final class DirectSettledFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false

    var isSet: Bool {
        lock.withLock { value }
    }

    func set() {
        lock.withLock { value = true }
    }
}

extension NWConnection {
    /// The next bytes. `isComplete` once the peer has finished sending.
    func receiveChunk(
        maximumLength: Int
    ) async throws -> (data: Data, isComplete: Bool) {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                self.receive(
                    minimumIncompleteLength: 1,
                    maximumLength: maximumLength
                ) { data, _, isComplete, error in
                    if let error {
                        continuation.resume(throwing: error)
                    } else {
                        continuation.resume(
                            returning: (data ?? Data(), isComplete)
                        )
                    }
                }
            }
        } onCancel: {
            self.cancel()
        }
    }

    /// Returns once the stack has taken `data`.
    func sendAll(_ data: Data) async throws {
        try await sendChunk(data, isFinal: false)
    }

    /// Tells the peer this side has no more bytes.
    func finishSending() async throws {
        try await sendChunk(nil, isFinal: true)
    }

    private func sendChunk(_ data: Data?, isFinal: Bool) async throws {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation {
                (continuation: CheckedContinuation<Void, Error>) in
                self.send(
                    content: data,
                    contentContext: isFinal ? .finalMessage : .defaultMessage,
                    isComplete: isFinal,
                    completion: .contentProcessed { error in
                        if let error {
                            continuation.resume(throwing: error)
                        } else {
                            continuation.resume()
                        }
                    }
                )
            }
        } onCancel: {
            self.cancel()
        }
    }
}

/// WebKit's side of one connection after its request head.
///
/// The commit point is `startTunnel()`, called after a backend is chosen
/// and before the `200` is written. Every byte delivered before it is
/// early: the bytes after the head, and every receive that completes
/// before it. The reader receives on its own until then, whoever reads, and
/// counts them against the early bound; past it, it fails closed: the bytes
/// are dropped, every read throws `earlyOverflow`, and the tunnel cannot
/// start. So no watcher being cancelled lets more than the bound be held
/// before a backend is chosen. A receive that completes after the commit
/// point belongs to the tunnel, whenever it was issued: WebKit may already
/// have its `200`, and its next bytes are the tunnel's, of any size. After
/// the commit point the reader receives only while someone reads, so a slow
/// consumer slows WebKit.
final class DirectClientReader: @unchecked Sendable {
    /// One receive: up to `maximumLength` bytes, then the completion with
    /// the bytes, whether the peer finished, and an error.
    typealias Receive = @Sendable (
        _ maximumLength: Int,
        _ completion: @escaping @Sendable (Data?, Bool, Error?) -> Void
    ) -> Void

    private let receiveChunk: Receive
    private let maximumLength: Int
    private let earlyLimit: Int
    private let lock = NSLock()
    private var chunks: [Data] = []
    private var ended = false
    private var failure: Error?
    private var receiving = false
    private var waiter: CheckedContinuation<Data?, Error>?
    private var goneWaiter: CheckedContinuation<Void, Error>?
    private var tunnelStarted = false
    private var early: Int

    init(
        maximumLength: Int,
        earlyLimit: Int,
        initial: Data,
        receive: @escaping Receive
    ) {
        receiveChunk = receive
        self.maximumLength = maximumLength
        self.earlyLimit = earlyLimit
        early = initial.count
        if initial.count > earlyLimit {
            ended = true
            failure = DirectConnectError.earlyOverflow
        } else if !initial.isEmpty {
            chunks = [initial]
        }
    }

    convenience init(
        _ connection: NWConnection,
        maximumLength: Int,
        earlyLimit: Int,
        initial: Data
    ) {
        self.init(
            maximumLength: maximumLength,
            earlyLimit: earlyLimit,
            initial: initial
        ) { maximumLength, completion in
            connection.receive(
                minimumIncompleteLength: 1,
                maximumLength: maximumLength
            ) { data, _, isComplete, error in
                completion(data, isComplete, error)
            }
        }
    }

    /// Starts receiving before the tunnel.
    func start() {
        let issue: Bool = lock.withLock {
            guard !receiving, !ended, !tunnelStarted else {
                return false
            }
            receiving = true
            return true
        }
        if issue {
            receive()
        }
    }

    /// The commit point: every byte delivered from here on is the tunnel's.
    /// `false` when WebKit's side has already ended or overflowed: the
    /// tunnel must not start.
    func startTunnel() -> Bool {
        lock.withLock {
            guard !ended else {
                return false
            }
            tunnelStarted = true
            return true
        }
    }

    /// Returns once WebKit's side has ended or overflowed before the tunnel.
    /// Throws `CancellationError` when cancelled.
    func waitUntilGone() async throws {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                // Decided under the lock: the continuation is either
                // resumed here or registered for exactly one resumer.
                let now: Result<Void, Error>? = lock.withLock {
                    if ended {
                        return .success(())
                    }
                    if Task.isCancelled {
                        return .failure(CancellationError())
                    }
                    goneWaiter = continuation
                    return nil
                }
                if let now {
                    continuation.resume(with: now)
                }
            }
        } onCancel: {
            let waiting = lock.withLock { () -> CheckedContinuation<Void, Error>? in
                let waiting = goneWaiter
                goneWaiter = nil
                return waiting
            }
            waiting?.resume(throwing: CancellationError())
        }
    }

    /// The next bytes, or `nil` once WebKit has finished sending.
    func read() async throws -> Data? {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                let action: () -> Void = lock.withLock {
                    if !chunks.isEmpty {
                        let data = chunks.removeFirst()
                        return { continuation.resume(returning: data) }
                    }
                    if ended {
                        let failure = failure
                        return {
                            if let failure {
                                continuation.resume(throwing: failure)
                            } else {
                                continuation.resume(returning: nil)
                            }
                        }
                    }
                    if Task.isCancelled {
                        return { continuation.resume(throwing: CancellationError()) }
                    }
                    waiter = continuation
                    guard !receiving else {
                        return {}
                    }
                    receiving = true
                    return { self.receive() }
                }
                action()
            }
        } onCancel: {
            let waiting: CheckedContinuation<Data?, Error>? = lock.withLock {
                let waiting = waiter
                waiter = nil
                return waiting
            }
            waiting?.resume(throwing: CancellationError())
        }
    }

    private func receive() {
        receiveChunk(maximumLength) { [self] data, isComplete, error in
            var resumes: [() -> Void] = []
            var again = false
            lock.withLock {
                receiving = false
                guard !ended else {
                    return
                }
                // Early or the tunnel's is decided by when the bytes are
                // delivered, against the commit point, under this lock.
                let isEarly = !tunnelStarted
                if let data, !data.isEmpty {
                    if isEarly {
                        early += data.count
                    }
                    if isEarly, early > earlyLimit {
                        // Fail closed: nothing of WebKit's is kept.
                        chunks.removeAll()
                        ended = true
                        failure = DirectConnectError.earlyOverflow
                    } else {
                        chunks.append(data)
                    }
                }
                if !ended {
                    if isComplete {
                        ended = true
                    } else if let error {
                        ended = true
                        failure = error
                    }
                }
                if ended, let gone = goneWaiter {
                    goneWaiter = nil
                    resumes.append { gone.resume() }
                }
                if let waiting = waiter {
                    if !chunks.isEmpty {
                        waiter = nil
                        let next = chunks.removeFirst()
                        resumes.append { waiting.resume(returning: next) }
                    } else if ended {
                        waiter = nil
                        let failure = failure
                        resumes.append {
                            if let failure {
                                waiting.resume(throwing: failure)
                            } else {
                                waiting.resume(returning: nil)
                            }
                        }
                    }
                }
                // Before the tunnel: keep receiving, bounded by the early
                // limit. After it: only for a waiting read.
                if !ended, !tunnelStarted || waiter != nil {
                    receiving = true
                    again = true
                }
            }
            for resume in resumes {
                resume()
            }
            if again {
                receive()
            }
        }
    }
}

/// One race of one plan. Retiring it cancels the race; the CONNECT is then
/// planned again rather than ended.
final class DirectRaceHandle: @unchecked Sendable {
    private let lock = NSLock()
    private var retired = false
    private var cancel: (@Sendable () -> Void)?

    var isRetired: Bool {
        lock.withLock { retired }
    }

    /// What retiring cancels. Runs at once if already retired.
    func attach(_ cancel: @escaping @Sendable () -> Void) {
        let alreadyRetired: Bool = lock.withLock {
            self.cancel = cancel
            return retired
        }
        if alreadyRetired {
            cancel()
        }
    }

    func retire() {
        let cancel: (@Sendable () -> Void)? = lock.withLock {
            retired = true
            return self.cancel
        }
        cancel?()
    }
}

/// A listener's socket state, and the one place where "a route was
/// published to this listener" and "its socket failed" are ordered.
///
/// Network reports a failure on its own queue; `failed(generation:)`
/// records it there, under this lock, before the failure callback goes
/// anywhere. The path controller calls `claimPublication()` immediately
/// before it writes a route, with no suspension between the two. The lock
/// decides which came first, and nothing can change that afterwards:
/// - a failure recorded first makes the claim fail, and no route is written:
///   the failure is one before any route;
/// - a claim made first means the failure, whenever it comes, is one after a
///   route (`failed` returns `true`), which is the listener fault.
final class DirectSocketHealth: @unchecked Sendable {
    private let lock = NSLock()
    private var serving: Int?
    private var published = false

    /// The socket of bind `generation` is ready.
    func serving(generation: Int) {
        lock.withLock { serving = generation }
    }

    /// The socket of bind `generation` failed. Returns whether a route had
    /// been published to this listener before. A failure of an earlier
    /// socket changes nothing.
    @discardableResult
    func failed(generation: Int) -> Bool {
        lock.withLock {
            if serving == generation {
                serving = nil
            }
            return published
        }
    }

    /// The listening socket was closed on purpose.
    func closed() {
        lock.withLock { serving = nil }
    }

    /// A route is about to be published to this listener. `false` when its
    /// socket is not serving: no route may be written.
    func claimPublication() -> Bool {
        lock.withLock {
            guard serving != nil else {
                return false
            }
            published = true
            return true
        }
    }

    var isServing: Bool {
        lock.withLock { serving != nil }
    }
}


/// Decides, once, whether a relay ended on its own or was closed by an
/// event. Whichever call comes first wins; the other learns it lost.
final class DirectRelayClaim: @unchecked Sendable {
    private enum State {
        case open
        case ended
        case closed
    }

    private let lock = NSLock()
    private var state = State.open

    /// The relay ended on its own. `false` if a close came first.
    @discardableResult
    func end() -> Bool {
        lock.withLock {
            guard state == .open else {
                return false
            }
            state = .ended
            return true
        }
    }

    /// An event closes the relay. `false` if it had already ended.
    func closeByEvent() -> Bool {
        lock.withLock {
            guard state == .open else {
                return false
            }
            state = .closed
            return true
        }
    }
}
